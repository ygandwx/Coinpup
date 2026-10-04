"""Append-only corrections and terminal cancellations of existing financial operations."""

from uuid import uuid4

from pydantic import TypeAdapter, ValidationError
from sqlalchemy import func, insert, select

from coinpup_api.ledger.balances import BalanceQueries
from coinpup_api.ledger.commands.classified import ClassifiedCommands
from coinpup_api.ledger.commands.exchange import ExchangeCommands
from coinpup_api.ledger.commands.fees import FeeCommands
from coinpup_api.ledger.commands.transfer import TransferCommands
from coinpup_api.ledger.common import PostingCore, _invalid_money, asset_definition
from coinpup_api.ledger.errors import MoneyError
from coinpup_api.ledger.idempotency import (
    _IDEMPOTENCY_KEY,
    CommandIdempotency,
    receipt_hash_version,
    revision_hash_v2,
)
from coinpup_api.ledger.idempotency import revision_hash as revision_hash
from coinpup_api.ledger.models import (
    AssetRecord,
    CommandReceipt,
    FinancialOperation,
    Journal,
    JournalLine,
)
from coinpup_api.ledger.money import Amount
from coinpup_api.ledger.posting_schemas import HistoryEntry, JournalAudit, LineAudit, OperationState
from coinpup_api.ledger.posting_storage import (
    PostingLine,
    prepare_journal_lines,
    prepare_reversal_lines,
)
from coinpup_api.ledger.readers import PostingReaders
from coinpup_api.ledger.readers import operation_state as operation_state
from coinpup_api.ledger.service import LedgerError, _not_found, _page, _version

_STATE = TypeAdapter(OperationState)


def prepare_replacement(service, session, ledger_id, journal_id, payload, catalog):
    """Prepare a complete replacement with the same active-target rules as creation."""
    kind = payload.kind
    if kind == "exchange":
        if payload.source_asset_id == payload.destination_asset_id:
            raise LedgerError("same_asset", 422, "Exchange assets must be different.")
        service._account(session, ledger_id, payload.source_account_id, payload.source_asset_id)
        service._account(
            session, ledger_id, payload.destination_account_id, payload.destination_asset_id
        )
        source = Amount.parse(payload.source_amount, catalog[payload.source_asset_id])
        destination = Amount.parse(
            payload.destination_amount, catalog[payload.destination_asset_id]
        )
        if source.minor_units <= 0 or destination.minor_units <= 0:
            raise MoneyError("amount_positive", "Exchange quantities must be positive.")
        lines = [
            PostingLine(
                journal_id,
                ledger_id,
                1,
                "account",
                payload.source_asset_id,
                -source,
                account_id=payload.source_account_id,
            ),
            PostingLine(journal_id, ledger_id, 2, "exchange", payload.source_asset_id, source),
            PostingLine(
                journal_id,
                ledger_id,
                3,
                "account",
                payload.destination_asset_id,
                destination,
                account_id=payload.destination_account_id,
            ),
            PostingLine(
                journal_id, ledger_id, 4, "exchange", payload.destination_asset_id, -destination
            ),
        ]
    else:
        quantity = Amount.parse(payload.amount, catalog[payload.asset_id])
        if quantity.minor_units == 0 or (kind != "opening" and quantity.minor_units < 0):
            raise MoneyError(
                "amount_positive", "A non-zero opening or positive principal is required."
            )
        if kind == "transfer":
            if payload.source_account_id == payload.destination_account_id:
                raise LedgerError("same_account", 422, "Transfer accounts must be different.")
            service._account(session, ledger_id, payload.source_account_id, payload.asset_id)
            service._account(session, ledger_id, payload.destination_account_id, payload.asset_id)
            lines = [
                PostingLine(
                    journal_id,
                    ledger_id,
                    1,
                    "account",
                    payload.asset_id,
                    -quantity,
                    account_id=payload.source_account_id,
                ),
                PostingLine(
                    journal_id,
                    ledger_id,
                    2,
                    "account",
                    payload.asset_id,
                    quantity,
                    account_id=payload.destination_account_id,
                ),
            ]
        else:
            service._account(session, ledger_id, payload.account_id, payload.asset_id)
            lines = [
                PostingLine(
                    journal_id,
                    ledger_id,
                    1,
                    "account",
                    payload.asset_id,
                    -quantity if kind == "expense" else quantity,
                    account_id=payload.account_id,
                )
            ]
            if kind == "opening":
                lines.append(
                    PostingLine(journal_id, ledger_id, 2, "equity", payload.asset_id, -quantity)
                )
            else:
                splits = service._splits(session, ledger_id, payload, kind, quantity)
                lines.extend(
                    PostingLine(
                        journal_id,
                        ledger_id,
                        index,
                        kind,
                        payload.asset_id,
                        -amount if kind == "income" else amount,
                        category_id=split.category_id,
                    )
                    for index, (split, amount) in enumerate(splits, 2)
                )
    service._append_fees(session, ledger_id, lines, getattr(payload, "fees", []), catalog)
    recognition_date = (
        payload.recognition_date if kind in {"income", "expense"} else payload.transaction_date
    )
    return lines, prepare_journal_lines(lines, catalog), recognition_date


class RevisionService(
    ClassifiedCommands,
    TransferCommands,
    ExchangeCommands,
    FeeCommands,
    CommandIdempotency,
    PostingReaders,
    BalanceQueries,
    PostingCore,
):
    def correct(self, owner_id, ledger_id, operation_id, payload, key, *, raw_body=None):
        return self._revise(
            owner_id, ledger_id, operation_id, "correct", payload, key, raw_body=raw_body
        )

    def cancel(self, owner_id, ledger_id, operation_id, payload, key, *, raw_body=None):
        return self._revise(
            owner_id, ledger_id, operation_id, "cancel", payload, key, raw_body=raw_body
        )

    def _revise(self, owner_id, ledger_id, operation_id, action, payload, key, *, raw_body=None):
        if not isinstance(key, str) or _IDEMPOTENCY_KEY.fullmatch(key) is None:
            raise LedgerError(
                "invalid_idempotency_key",
                422,
                "Use 1–128 visible ASCII characters for the command key.",
            )
        with self._transaction(owner_id, write=True) as session:
            _, entity = self._locked_ledger(session, owner_id, ledger_id)
            receipt = session.get(CommandReceipt, (ledger_id, key))
            hash_version = receipt_hash_version(receipt)
            digest = (
                revision_hash(action, ledger_id, operation_id, payload)
                if hash_version == 1
                else revision_hash_v2(action, ledger_id, operation_id, payload, raw_body=raw_body)
            )
            if receipt is not None:
                if receipt.request_hash != digest:
                    raise LedgerError(
                        "idempotency_conflict",
                        409,
                        "This command key was used for a different request.",
                    )
                try:
                    return _STATE.validate_python(receipt.response)
                except ValidationError:
                    raise LedgerError(
                        "ledger_integrity", 503, "The stored revision receipt is inconsistent."
                    ) from None
            if entity.archived:
                raise LedgerError(
                    "entity_archived", 409, "Restore the entity before changing its ledger."
                )
            operation = session.scalar(
                select(FinancialOperation)
                .where(
                    FinancialOperation.id == operation_id,
                    FinancialOperation.ledger_id == ledger_id,
                )
                .with_for_update()
            )
            if operation is None:
                raise _not_found()
            _version(operation, payload.expected_version)
            if operation.status == "cancelled":
                raise LedgerError(
                    "operation_cancelled", 409, "A cancelled operation cannot be changed."
                )
            original = session.get(Journal, operation.current_journal_id)
            if original is None:
                raise LedgerError(
                    "ledger_integrity", 503, "The original posting journal is missing."
                )
            old_lines = session.scalars(
                select(JournalLine)
                .where(
                    JournalLine.journal_id == original.id,
                    JournalLine.ledger_id == ledger_id,
                )
                .order_by(JournalLine.line_no)
            ).all()
            replacement = payload.replacement if action == "correct" else None
            new_assets = set()
            if replacement is not None:
                if replacement.kind != operation.kind:
                    raise LedgerError(
                        "operation_kind_mismatch",
                        422,
                        "A replacement must keep the operation kind.",
                    )
                if operation.kind == "opening":
                    account_line = next(
                        line
                        for line in old_lines
                        if line.component_no == 0 and line.role == "account"
                    )
                    if (replacement.account_id, replacement.asset_id) != (
                        account_line.account_id,
                        account_line.asset_id,
                    ):
                        raise LedgerError(
                            "opening_identity",
                            409,
                            "An opening replacement must keep its account and asset.",
                        )
                new_assets = (
                    {replacement.source_asset_id, replacement.destination_asset_id}
                    if operation.kind == "exchange"
                    else {replacement.asset_id}
                )
                new_assets.update(fee.asset_id for fee in getattr(replacement, "fees", []))
            identifiers = {line.asset_id for line in old_lines} | new_assets
            records = session.scalars(
                select(AssetRecord)
                .where(AssetRecord.asset_id.in_(identifiers))
                .order_by(AssetRecord.asset_id)
                .with_for_update(read=True)
            ).all()
            if len(records) != len(identifiers):
                raise _not_found()
            catalog = {record.asset_id: asset_definition(record) for record in records}
            if any(not catalog[identifier].enabled for identifier in new_assets):
                raise LedgerError("asset_disabled", 409, "An asset is disabled for new use.")
            reversal_id, replacement_id = uuid4(), uuid4()
            try:
                reversed_lines, reversed_parameters = prepare_reversal_lines(
                    old_lines, reversal_id, catalog
                )
                new_lines, new_parameters, recognition_date = [], [], None
                if replacement is not None:
                    new_lines, new_parameters, recognition_date = prepare_replacement(
                        self, session, ledger_id, replacement_id, replacement, catalog
                    )
                self._validate_balance_deltas(
                    session, ledger_id, [*reversed_lines, *new_lines], catalog
                )
            except MoneyError as error:
                raise _invalid_money(error) from None
            version = operation.version + 1
            now = session.scalar(select(func.clock_timestamp()))
            session.execute(
                insert(Journal).values(
                    id=reversal_id,
                    operation_id=operation.id,
                    ledger_id=ledger_id,
                    operation_version=version,
                    journal_kind="reversal",
                    reverses_journal_id=original.id,
                    transaction_date=original.transaction_date,
                    recognition_date=original.recognition_date,
                    description=original.description,
                    revision_reason=payload.reason,
                    created_at=now,
                )
            )
            session.execute(insert(JournalLine), reversed_parameters)
            if replacement is not None:
                session.execute(
                    insert(Journal).values(
                        id=replacement_id,
                        operation_id=operation.id,
                        ledger_id=ledger_id,
                        operation_version=version,
                        journal_kind="posting",
                        transaction_date=replacement.transaction_date,
                        recognition_date=recognition_date,
                        description=replacement.description,
                        revision_reason=payload.reason,
                        created_at=now,
                    )
                )
                session.execute(insert(JournalLine), new_parameters)
                operation.current_journal_id = replacement_id
            operation.version = version
            operation.status = "active" if replacement is not None else "cancelled"
            operation.updated_at = now
            session.flush()
            session.refresh(operation)
            response = operation_state(session, operation)
            session.execute(
                insert(CommandReceipt).values(
                    ledger_id=ledger_id,
                    key=key,
                    request_hash=digest,
                    hash_version=2,
                    response=response.model_dump(mode="json"),
                    response_status=200,
                    operation_id=operation.id,
                    created_at=now,
                )
            )
            return response

    def read_history(self, owner_id, ledger_id, operation_id, limit=100, offset=0):
        _page(limit, offset)
        with self._transaction(owner_id) as session:
            self._ledger(session, owner_id, ledger_id)
            operation = session.scalar(
                select(FinancialOperation).where(
                    FinancialOperation.id == operation_id, FinancialOperation.ledger_id == ledger_id
                )
            )
            if operation is None:
                raise _not_found()
            versions = session.scalars(
                select(Journal.operation_version)
                .where(Journal.operation_id == operation_id, Journal.ledger_id == ledger_id)
                .distinct()
                .order_by(Journal.operation_version)
                .limit(limit)
                .offset(offset)
            ).all()
            journals = session.scalars(
                select(Journal).where(
                    Journal.operation_id == operation_id,
                    Journal.ledger_id == ledger_id,
                    Journal.operation_version.in_(versions),
                )
            ).all()
            result = []
            for version in versions:
                revision = sorted(
                    [journal for journal in journals if journal.operation_version == version],
                    key=lambda journal: journal.journal_kind != "reversal",
                )
                action = "create" if version == 1 else "correct" if len(revision) == 2 else "cancel"
                audits = []
                for journal in revision:
                    lines = session.scalars(
                        select(JournalLine)
                        .where(JournalLine.journal_id == journal.id)
                        .order_by(JournalLine.line_no)
                    ).all()
                    output = []
                    for line in lines:
                        definition = asset_definition(session.get(AssetRecord, line.asset_id))
                        try:
                            amount = Amount.from_decimal(line.amount, definition).to_string()
                        except MoneyError:
                            raise LedgerError(
                                "ledger_integrity", 503, "Stored audit quantities are inconsistent."
                            ) from None
                        output.append(
                            LineAudit(
                                id=line.id,
                                line_no=line.line_no,
                                component_no=line.component_no,
                                role=line.role,
                                asset_id=line.asset_id,
                                amount=amount,
                                account_id=line.account_id,
                                category_id=line.category_id,
                            )
                        )
                    audits.append(
                        JournalAudit(
                            id=journal.id,
                            kind=journal.journal_kind,
                            reverses_journal_id=journal.reverses_journal_id,
                            transaction_date=journal.transaction_date,
                            recognition_date=journal.recognition_date,
                            description=journal.description,
                            reason=journal.revision_reason,
                            recorded_at=journal.created_at,
                            lines=output,
                        )
                    )
                result.append(
                    HistoryEntry(
                        version=version,
                        action=action,
                        actor_id=operation.created_by,
                        reason=revision[0].revision_reason,
                        recorded_at=revision[0].created_at,
                        journals=audits,
                    )
                )
            return result

    def correct_operation(
        self,
        owner_id,
        ledger_id,
        operation_id,
        payload,
        idempotency_key,
        *,
        raw_body: bytes | None = None,
    ):
        return RevisionService(self.engine).correct(
            owner_id, ledger_id, operation_id, payload, idempotency_key, raw_body=raw_body
        )

    def cancel_operation(
        self,
        owner_id,
        ledger_id,
        operation_id,
        payload,
        idempotency_key,
        *,
        raw_body: bytes | None = None,
    ):
        return RevisionService(self.engine).cancel(
            owner_id, ledger_id, operation_id, payload, idempotency_key, raw_body=raw_body
        )

    def history(self, owner_id, ledger_id, operation_id, limit=100, offset=0):
        return RevisionService(self.engine).read_history(
            owner_id, ledger_id, operation_id, limit, offset
        )
