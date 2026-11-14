"""Atomic correction or cancellation of one actual refund under its fixed source payment."""

from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

from sqlalchemy import func, insert, select, update

from coinpup_api.business.allocation_return_models import BusinessAllocationReturn
from coinpup_api.business.allocation_return_sources import require_current
from coinpup_api.business.refund_plan import RefundReturnSource, plan_refund
from coinpup_api.business.refund_revision_current import read_current_refund
from coinpup_api.business.refund_revision_hash import refund_revision_hash_v2
from coinpup_api.business.refund_revision_models import BusinessRefundRevision as Revision
from coinpup_api.business.refund_revision_receipts import (
    integrity,
    replay_revision,
    response_from_rows,
)
from coinpup_api.business.refund_revision_return_models import (
    BusinessRefundRevisionReturn as Evidence,
)
from coinpup_api.business.refund_rows import refund_rows
from coinpup_api.business.refund_schemas import BusinessRefundCreate
from coinpup_api.business.refunds import BusinessRefundService
from coinpup_api.business.settlement_plan import PaymentSource
from coinpup_api.business.settlement_rows import PostingGroup
from coinpup_api.business.settlement_totals import payment_totals
from coinpup_api.ledger.common import _invalid_money, asset_definition
from coinpup_api.ledger.errors import MoneyError
from coinpup_api.ledger.idempotency import _IDEMPOTENCY_KEY
from coinpup_api.ledger.models import (
    Account,
    AccountAsset,
    AssetRecord,
    CommandReceipt,
    FinancialOperation,
    Journal,
    JournalLine,
)
from coinpup_api.ledger.money import Amount
from coinpup_api.ledger.period_checks import require_open_period
from coinpup_api.ledger.posting_storage import prepare_reversal_lines
from coinpup_api.ledger.service import LedgerError, _not_found, _version


def _catalog(session, current, replacement):
    old = {line.asset_id for line in current.lines}
    new = set()
    if replacement:
        new = {
            current.payment.asset_id,
            replacement.asset_id,
            *(fee.asset_id for fee in replacement.fees),
        }
    assets = old | new
    rows = session.scalars(
        select(AssetRecord)
        .where(AssetRecord.asset_id.in_(assets))
        .order_by(AssetRecord.asset_id)
        .with_for_update(read=True)
    )
    catalog = {row.asset_id: asset_definition(row) for row in rows}
    if not old.issubset(catalog):
        raise integrity()
    if not new.issubset(catalog):
        raise _not_found()
    if any(not catalog[key].enabled for key in new):
        raise LedgerError("asset_disabled", 409, "An asset is disabled for new use.")
    return catalog


def _control(session, current):
    payment = current.payment
    key = "advance.received" if payment.direction == "receipt" else "advance.paid"
    previous_accounts = {
        line.account_id
        for line in current.lines
        if line.component_no == 0 and line.role == "account"
    }
    row = session.execute(
        select(Account, AccountAsset)
        .join(
            AccountAsset,
            (Account.id == AccountAsset.account_id) & (Account.ledger_id == AccountAsset.ledger_id),
        )
        .where(
            Account.ledger_id == payment.ledger_id,
            Account.id.in_(previous_accounts),
            Account.system_key == key,
            AccountAsset.asset_id == payment.asset_id,
        )
    ).one_or_none()
    if row is None:
        raise integrity()
    account, link = row
    if account.archived:
        raise LedgerError("account_archived", 409, "Restore the account before new use.")
    if not link.enabled:
        raise LedgerError("account_asset_disabled", 409, "Restore the asset link before new use.")
    return {key: account}


class BusinessRefundRevisionService(BusinessRefundService):
    def correct(self, owner_id, ledger_id, refund_id, payload, key, *, raw_body=None):
        return self._revise(owner_id, ledger_id, refund_id, "correct", payload, key, raw_body)

    def cancel(self, owner_id, ledger_id, refund_id, payload, key, *, raw_body=None):
        return self._revise(owner_id, ledger_id, refund_id, "cancel", payload, key, raw_body)

    def _revise(self, owner_id, ledger_id, refund_id, action, payload, key, raw_body):
        if not isinstance(key, str) or _IDEMPOTENCY_KEY.fullmatch(key) is None:
            raise LedgerError("invalid_idempotency_key", 422, "Use 1–128 visible ASCII characters.")
        with self._transaction(owner_id, write=True, read_committed=True) as session:
            _, entity = self._locked_ledger(session, owner_id, ledger_id)
            digest = refund_revision_hash_v2(
                action, ledger_id, refund_id, payload, raw_body=raw_body
            )
            receipt = session.get(CommandReceipt, (ledger_id, key))
            if receipt is not None:
                return replay_revision(
                    session, receipt, ledger_id, refund_id, action, payload, digest
                )
            if entity.archived:
                raise LedgerError("entity_archived", 409, "Restore the entity before posting.")
            current = read_current_refund(session, ledger_id, refund_id)
            _version(current.operation, payload.expected_version)
            if current.operation.status == "cancelled":
                raise LedgerError("refund_cancelled", 409, "A cancelled refund cannot be changed.")
            if current.operation.version >= 2147483647:
                raise LedgerError(
                    "refund_revision_version", 409, "The refund version cannot advance."
                )
            require_current(
                current.payment_operation,
                current.refund.source_version,
                current.payment.journal_id,
                "payment",
            )
            replacement = payload.replacement if action == "correct" else None
            catalog = _catalog(session, current, replacement)
            dates = [current.journal]
            if replacement:
                dates.append(
                    SimpleNamespace(
                        transaction_date=replacement.transaction_date,
                        recognition_date=replacement.transaction_date,
                    )
                )
            require_open_period(session, ledger_id, *dates)
            try:
                reversal_id = uuid4()
                lines, parameters = prepare_reversal_lines(current.lines, reversal_id, catalog)
                reversal = PostingGroup(
                    current.operation.id,
                    reversal_id,
                    current.journal.description,
                    tuple(lines),
                    tuple(parameters),
                )
                posting = (
                    self._replacement(session, current, replacement, catalog)
                    if replacement
                    else None
                )
                combined = (*reversal.lines, *posting.lines) if posting else reversal.lines
                self._validate_balance_deltas(session, ledger_id, combined, catalog)
            except MoneyError as error:
                raise _invalid_money(error) from None
            now = session.scalar(select(func.clock_timestamp()))
            identifier = _persist(
                session, current, payload, key, owner_id, reversal, posting, catalog, now
            )
            result = response_from_rows(session, identifier)
            session.execute(
                insert(CommandReceipt).values(
                    ledger_id=ledger_id,
                    key=key,
                    hash_version=2,
                    request_hash=digest,
                    response_status=200,
                    operation_id=current.operation.id,
                    response=result.model_dump(mode="json"),
                    created_at=now,
                )
            )
            return result

    def _replacement(self, session, current, replacement, catalog):
        payment = current.payment
        ledger_id = payment.ledger_id
        controls = _control(session, current)
        self._account(session, ledger_id, replacement.account_id, replacement.asset_id)
        references = session.scalars(
            select(BusinessAllocationReturn).where(
                BusinessAllocationReturn.ledger_id == ledger_id,
                BusinessAllocationReturn.payment_id == payment.id,
                BusinessAllocationReturn.id.in_(replacement.allocation_return_ids),
            )
        ).all()
        if len(references) != len(replacement.allocation_return_ids):
            raise _not_found()
        sources = {
            r.id: RefundReturnSource(r.id, ledger_id, payment.id, r.return_date) for r in references
        }
        asset = catalog[payment.asset_id]
        applied, total = payment_totals(session, ledger_id, {payment.id}).get(
            payment.id, (Decimal(0), Decimal(0))
        )
        other = Amount.from_decimal(total, asset) - Amount.from_decimal(
            Decimal(current.fact.payment_amount), asset
        )
        source = PaymentSource(
            payment.id,
            ledger_id,
            payment.party_id,
            payment.direction,
            payment.transaction_date,
            Amount.from_decimal(payment.amount, asset),
            Amount.from_decimal(applied, asset),
        )
        command = BusinessRefundCreate.model_validate(
            replacement.model_dump()
            | dict(
                id=current.refund.id,
                payment_id=payment.id,
                expected_version=current.refund.source_version,
            )
        )
        plan = plan_refund(ledger_id, source, other, command, sources, catalog)
        return refund_rows(
            self,
            session,
            ledger_id,
            payment.party_id,
            command,
            plan,
            controls,
            catalog,
            operation_id=current.operation.id,
        )


def _persist(session, current, payload, key, owner_id, reversal, posting, catalog, now):
    identifier = uuid4()
    op = current.operation
    old = current.journal
    version = op.version + 1
    ledger_id = op.ledger_id
    facts = {}
    if posting is not None:
        replacement = payload.replacement
        facts = dict(
            account_id=replacement.account_id,
            asset_id=replacement.asset_id,
            amount=Amount.parse(replacement.amount, catalog[replacement.asset_id]).to_decimal(),
            payment_amount=Amount.parse(
                replacement.payment_amount, catalog[current.payment.asset_id]
            ).to_decimal(),
            reason=replacement.reason,
            transaction_date=replacement.transaction_date,
            fees=[fee.model_dump(mode="json") for fee in replacement.fees],
            replacement_journal_id=posting.journal_id,
        )
    session.execute(
        insert(Revision).values(
            id=identifier,
            ledger_id=ledger_id,
            refund_id=current.refund.id,
            payment_id=current.payment.id,
            operation_id=op.id,
            operation_version=version,
            action="correct" if posting else "cancel",
            previous_revision_id=current.revision.id if current.revision else None,
            previous_journal_id=old.id,
            reversal_journal_id=reversal.journal_id,
            revision_reason=payload.reason,
            created_by=owner_id,
            idempotency_key=key,
            created_at=now,
            **facts,
        )
    )
    if posting and payload.replacement.allocation_return_ids:
        session.execute(
            insert(Evidence),
            [
                dict(
                    id=uuid4(),
                    revision_id=identifier,
                    refund_id=current.refund.id,
                    payment_id=current.payment.id,
                    ledger_id=ledger_id,
                    return_id=source,
                    return_version=1,
                    line_no=i,
                    created_at=now,
                )
                for i, source in enumerate(payload.replacement.allocation_return_ids, 1)
            ],
        )
    for group in (reversal, posting):
        if group is None:
            continue
        reversing = group is reversal
        session.execute(
            insert(Journal).values(
                id=group.journal_id,
                ledger_id=ledger_id,
                operation_id=op.id,
                operation_version=version,
                journal_kind="reversal" if reversing else "posting",
                reverses_journal_id=old.id if reversing else None,
                description=group.description,
                transaction_date=old.transaction_date
                if reversing
                else payload.replacement.transaction_date,
                recognition_date=old.recognition_date
                if reversing
                else payload.replacement.transaction_date,
                revision_reason=payload.reason,
                created_at=now,
            )
        )
        session.execute(insert(JournalLine), list(group.parameters))
    session.execute(
        update(FinancialOperation)
        .where(FinancialOperation.id == op.id, FinancialOperation.ledger_id == ledger_id)
        .values(
            version=version,
            status="active" if posting else "cancelled",
            current_journal_id=posting.journal_id if posting else old.id,
        )
    )
    return identifier
