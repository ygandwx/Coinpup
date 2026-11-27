"""Atomic transfer between chain units and the corresponding platform custody units."""

from uuid import uuid4

from pydantic import ValidationError
from sqlalchemy import func, insert, or_, select

from coinpup_api.business.settlement_rows import PostingGroup, persist_posting
from coinpup_api.ledger.assets import custodial_family
from coinpup_api.ledger.balances import BalanceQueries
from coinpup_api.ledger.commands.fees import FeeCommands
from coinpup_api.ledger.common import PostingCore, _invalid_money, asset_definition
from coinpup_api.ledger.errors import MoneyError
from coinpup_api.ledger.idempotency import _IDEMPOTENCY_KEY, command_hash_v2, revision_hash_v2
from coinpup_api.ledger.models import (
    AssetRecord,
    CommandReceipt,
    Entity,
    FinancialOperation,
    Journal,
    JournalLine,
    Ledger,
    PlatformTransfer,
)
from coinpup_api.ledger.money import Amount
from coinpup_api.ledger.period_checks import require_open_period
from coinpup_api.ledger.platform_transfer_schemas import (
    PlatformPostingRef,
    PlatformTransferCancellation,
    PlatformTransferDetail,
    PlatformTransferReceipt,
)
from coinpup_api.ledger.posting_storage import (
    PostingLine,
    prepare_journal_lines,
    prepare_reversal_lines,
)
from coinpup_api.ledger.service import LedgerError, _not_found, _page, _version

OPERATION_KIND = "platform_move"
CLEARING_ROLE = "platform_clear"
_MODES = {"deposit": ("onchain", "custodial"), "withdrawal": ("custodial", "onchain")}


def _key(key):
    if not isinstance(key, str) or _IDEMPOTENCY_KEY.fullmatch(key) is None:
        raise LedgerError("invalid_idempotency_key", 422, "Use 1–128 visible ASCII characters.")


def _saved(record, schema):
    if record is not None and record.hash_version == 2:
        try:
            return schema.model_validate(record.response)
        except ValidationError:
            pass
    raise LedgerError("ledger_integrity", 503, "The stored platform transfer receipt is invalid.")


def _replay(session, ledger, key, digest, schema):
    receipt = session.get(CommandReceipt, (ledger, key))
    if receipt is None:
        return None
    if receipt.request_hash != digest:
        raise LedgerError(
            "idempotency_conflict", 409, "This command key was used for another request."
        )
    return _saved(receipt, schema)


def _save_receipt(session, ledger, key, digest, response, status, operation_id, now):
    session.execute(
        insert(CommandReceipt).values(
            ledger_id=ledger,
            key=key,
            request_hash=digest,
            hash_version=2,
            response=response.model_dump(mode="json"),
            response_status=status,
            operation_id=operation_id,
            created_at=now,
        )
    )


def _principal(ledger, journal, account, delta, first):
    return [
        PostingLine(
            journal,
            ledger,
            first,
            "account",
            delta.asset.asset_id,
            delta,
            account_id=account,
        ),
        PostingLine(journal, ledger, first + 1, CLEARING_ROLE, delta.asset.asset_id, -delta),
    ]


def _group(posting, description, lines, catalog):
    return PostingGroup(
        posting.operation_id,
        posting.journal_id,
        description,
        tuple(lines),
        tuple(prepare_journal_lines(lines, catalog)),
    )


def _locked_ledgers(session, owner, scope):
    ledgers = session.scalars(
        select(Ledger)
        .where(Ledger.owner_id == owner, Ledger.id.in_(scope))
        .order_by(Ledger.id)
        .with_for_update()
    ).all()
    if len(ledgers) != len(scope):
        raise _not_found()
    entity_ids = {ledger.entity_id for ledger in ledgers}
    entities = session.scalars(
        select(Entity)
        .where(Entity.owner_id == owner, Entity.id.in_(entity_ids))
        .order_by(Entity.id)
        .with_for_update()
    ).all()
    if len(entities) != len(entity_ids):
        raise _not_found()
    if any(entity.archived for entity in entities):
        raise LedgerError("entity_archived", 409, "Restore both entities before posting.")


def _status(session, item):
    original = _saved(
        session.get(CommandReceipt, (item.source_ledger_id, item.create_key)),
        PlatformTransferReceipt,
    )
    cancellation = None
    if item.cancel_key is not None:
        cancellation = _saved(
            session.get(CommandReceipt, (item.source_ledger_id, item.cancel_key)),
            PlatformTransferCancellation,
        )
    return PlatformTransferDetail(
        **original.model_dump(exclude={"status", "version"}),
        status="cancelled" if cancellation else "active",
        version=item.version,
        cancellation=cancellation,
    )


def _scope(owner, ledger):
    transfer = PlatformTransfer
    return select(transfer).where(
        transfer.owner_id == owner,
        or_(transfer.source_ledger_id == ledger, transfer.destination_ledger_id == ledger),
    )


class PlatformTransfers(FeeCommands, BalanceQueries, PostingCore):
    def create(self, owner, ledger, payload, key, *, raw_body=None):
        _key(key)
        digest = command_hash_v2("platform_transfer", ledger, payload, raw_body=raw_body)
        with self._transaction(owner, write=True, read_committed=True) as session:
            self._ledger(session, owner, ledger)
            replay = _replay(session, ledger, key, digest, PlatformTransferReceipt)
            if replay is not None:
                return replay
            target = payload.destination_ledger_id
            scopes = {ledger, target}
            _locked_ledgers(session, owner, scopes)
            for scope in scopes:
                require_open_period(session, scope, payload)
            if payload.source_account_id == payload.destination_account_id:
                raise LedgerError("same_account", 422, "Select different accounts.")
            catalog = self._catalog(
                session, (payload.source_asset_id, payload.destination_asset_id), payload.fees
            )
            source_asset = catalog[payload.source_asset_id]
            target_asset = catalog[payload.destination_asset_id]
            source = self._account(
                session, ledger, payload.source_account_id, source_asset.asset_id
            )
            destination = self._account(
                session, target, payload.destination_account_id, target_asset.asset_id
            )
            source_mode, target_mode = _MODES[payload.kind]
            if (source.custody_mode, destination.custody_mode) != (source_mode, target_mode):
                raise LedgerError(
                    "platform_custody_mismatch", 422, "Select the accounts for this direction."
                )
            family = custodial_family(source_asset)
            if (
                family is None
                or family != custodial_family(target_asset)
                or (source_asset.kind == "custodial") != (source_mode == "custodial")
                or (target_asset.kind == "custodial") != (target_mode == "custodial")
            ):
                raise LedgerError(
                    "platform_asset_mismatch",
                    422,
                    "Select verified assets of the same coin for both accounts.",
                )
            try:
                outgoing = Amount.parse(payload.amount, source_asset)
                incoming = Amount.parse(payload.amount, target_asset)
                if outgoing.minor_units <= 0:
                    raise MoneyError("amount_positive", "The quantity must be positive.")
                source_ref = PlatformPostingRef(
                    ledger_id=ledger, operation_id=uuid4(), journal_id=uuid4()
                )
                target_ref = (
                    source_ref
                    if ledger == target
                    else PlatformPostingRef(
                        ledger_id=target, operation_id=uuid4(), journal_id=uuid4()
                    )
                )
                first = _principal(ledger, source_ref.journal_id, source.id, -outgoing, 1)
                second = _principal(
                    target,
                    target_ref.journal_id,
                    destination.id,
                    incoming,
                    3 if ledger == target else 1,
                )
                if ledger == target:
                    first.extend(second)
                fees = self._append_fees(session, ledger, first, payload.fees, catalog)
                postings = [(source_ref, _group(source_ref, payload.description, first, catalog))]
                if ledger != target:
                    postings.append(
                        (target_ref, _group(target_ref, payload.description, second, catalog))
                    )
                for reference, group in postings:
                    self._validate_balance_deltas(
                        session, reference.ledger_id, group.lines, catalog
                    )
            except MoneyError as error:
                raise _invalid_money(error) from None

            now = session.scalar(select(func.clock_timestamp()))
            for reference, group in postings:
                persist_posting(
                    session,
                    owner,
                    reference.ledger_id,
                    OPERATION_KIND,
                    payload.transaction_date,
                    group,
                    now,
                )
            response = PlatformTransferReceipt(
                id=payload.id or uuid4(),
                kind=payload.kind,
                source_ledger_id=ledger,
                source_account_id=source.id,
                source_asset_id=source_asset.asset_id,
                source_amount=outgoing.to_string(),
                destination_ledger_id=target,
                destination_account_id=destination.id,
                destination_asset_id=target_asset.asset_id,
                destination_amount=incoming.to_string(),
                transaction_date=payload.transaction_date,
                description=payload.description,
                fees=fees,
                operations=[ref for ref, _ in postings],
                status="active",
                version=1,
            )
            _save_receipt(session, ledger, key, digest, response, 201, source_ref.operation_id, now)
            session.add(
                PlatformTransfer(
                    **response.model_dump(
                        exclude={
                            "source_amount",
                            "destination_amount",
                            "fees",
                            "operations",
                            "status",
                        }
                    ),
                    owner_id=owner,
                    amount=outgoing.to_decimal(),
                    source_operation_id=source_ref.operation_id,
                    destination_operation_id=target_ref.operation_id if ledger != target else None,
                    create_key=key,
                )
            )
            return response

    def cancel(self, owner, ledger, transfer_id, payload, key, *, raw_body=None):
        _key(key)
        digest = revision_hash_v2(
            "platform_cancel", ledger, transfer_id, payload, raw_body=raw_body
        )
        with self._transaction(owner, write=True, read_committed=True) as session:
            self._ledger(session, owner, ledger)
            replay = _replay(session, ledger, key, digest, PlatformTransferCancellation)
            if replay is not None:
                return replay
            item = session.scalar(
                _scope(owner, ledger).where(
                    PlatformTransfer.id == transfer_id, PlatformTransfer.source_ledger_id == ledger
                )
            )
            if item is None:
                raise _not_found()
            _version(item, payload.expected_version)
            if item.version != 1:
                raise LedgerError("operation_cancelled", 409, "This transfer is already cancelled.")
            _locked_ledgers(session, owner, {ledger, item.destination_ledger_id})
            operation_ids = {
                identifier
                for identifier in (item.source_operation_id, item.destination_operation_id)
                if identifier is not None
            }
            originals = session.scalars(
                select(Journal)
                .join(FinancialOperation, FinancialOperation.current_journal_id == Journal.id)
                .where(FinancialOperation.id.in_(operation_ids))
                .order_by(Journal.ledger_id, Journal.id)
            ).all()
            if len(originals) != len(operation_ids):
                raise LedgerError("ledger_integrity", 503, "A transfer member is missing.")
            lines = {}
            for original in originals:
                require_open_period(session, original.ledger_id, original)
                lines[original.id] = session.scalars(
                    select(JournalLine)
                    .where(JournalLine.journal_id == original.id)
                    .order_by(JournalLine.line_no)
                ).all()
            asset_ids = {line.asset_id for members in lines.values() for line in members}
            catalog = {
                row.asset_id: asset_definition(row)
                for row in session.scalars(
                    select(AssetRecord)
                    .where(AssetRecord.asset_id.in_(asset_ids))
                    .order_by(AssetRecord.asset_id)
                    .with_for_update(read=True)
                )
            }
            planned = []
            try:
                for original in originals:
                    reversal_id = uuid4()
                    inverse, parameters = prepare_reversal_lines(
                        lines[original.id], reversal_id, catalog
                    )
                    self._validate_balance_deltas(session, original.ledger_id, inverse, catalog)
                    planned.append((original, reversal_id, parameters))
            except MoneyError as error:
                raise _invalid_money(error) from None
            now = session.scalar(select(func.clock_timestamp()))
            reversals = []
            for original, reversal_id, parameters in planned:
                operation = session.get(FinancialOperation, original.operation_id)
                session.execute(
                    insert(Journal).values(
                        id=reversal_id,
                        operation_id=operation.id,
                        ledger_id=operation.ledger_id,
                        operation_version=2,
                        journal_kind="reversal",
                        reverses_journal_id=original.id,
                        transaction_date=original.transaction_date,
                        recognition_date=original.recognition_date,
                        description=original.description,
                        revision_reason=payload.reason,
                        created_at=now,
                    )
                )
                session.execute(insert(JournalLine), parameters)
                operation.status = "cancelled"
                operation.version = 2
                operation.updated_at = now
                reversals.append(
                    PlatformPostingRef(
                        ledger_id=operation.ledger_id,
                        operation_id=operation.id,
                        journal_id=reversal_id,
                    )
                )
            response = PlatformTransferCancellation(
                id=item.id,
                source_ledger_id=ledger,
                status="cancelled",
                version=2,
                reason=payload.reason,
                reversals=reversals,
            )
            _save_receipt(
                session, ledger, key, digest, response, 200, item.source_operation_id, now
            )
            item.version = 2
            item.cancel_key = key
            return response

    def detail(self, owner, ledger, transfer_id):
        with self._transaction(owner, read_only=True) as session:
            self._ledger(session, owner, ledger)
            item = session.scalar(_scope(owner, ledger).where(PlatformTransfer.id == transfer_id))
            if item is None:
                raise _not_found()
            return _status(session, item)

    def list(self, owner, ledger, limit=100, offset=0):
        _page(limit, offset)
        with self._transaction(owner, read_only=True) as session:
            self._ledger(session, owner, ledger)
            items = session.scalars(
                _scope(owner, ledger)
                .order_by(PlatformTransfer.transaction_date.desc(), PlatformTransfer.id.desc())
                .limit(limit)
                .offset(offset)
            )
            return [_status(session, item) for item in items]
