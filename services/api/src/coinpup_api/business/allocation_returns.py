"""Atomic explicit allocation returns with original-intent replay and no cash movement."""

from types import SimpleNamespace

from sqlalchemy import func, insert, select

from coinpup_api.business.allocation_return_models import (
    BusinessAllocationReturn,
    BusinessAllocationReturnLine,
)
from coinpup_api.business.allocation_return_plan import plan_allocation_return
from coinpup_api.business.allocation_return_receipts import replay_return, response_from_rows
from coinpup_api.business.allocation_return_sources import (
    original_sources,
    plan_sources,
    require_current,
)
from coinpup_api.business.settlement_models import BusinessPayment
from coinpup_api.business.settlement_rows import allocation_rows, persist_posting
from coinpup_api.ledger.balances import BalanceQueries
from coinpup_api.ledger.common import PostingCore, _invalid_money, asset_definition
from coinpup_api.ledger.control_accounts import ControlAccounts
from coinpup_api.ledger.errors import MoneyError
from coinpup_api.ledger.idempotency import _IDEMPOTENCY_KEY, _v2_hash
from coinpup_api.ledger.models import AssetRecord, CommandReceipt, FinancialOperation
from coinpup_api.ledger.period_checks import require_open_period
from coinpup_api.ledger.service import LedgerError, _not_found


class BusinessAllocationReturnService(BalanceQueries, PostingCore):
    def create(self, owner_id, ledger_id, payload, key, *, raw_body=None):
        if not isinstance(key, str) or _IDEMPOTENCY_KEY.fullmatch(key) is None:
            raise LedgerError("invalid_idempotency_key", 422, "Use 1–128 visible ASCII characters.")
        with self._transaction(owner_id, write=True, read_committed=True) as session:
            _, entity = self._locked_ledger(session, owner_id, ledger_id)
            digest = _v2_hash("business_allocation_return", ledger_id, payload, raw_body)
            receipt = session.get(CommandReceipt, (ledger_id, key))
            if receipt is not None:
                return replay_return(session, receipt, ledger_id, payload, digest)
            if entity.archived:
                raise LedgerError("entity_archived", 409, "Restore the entity before posting.")
            record = session.execute(
                select(BusinessPayment, FinancialOperation)
                .join(FinancialOperation, FinancialOperation.id == BusinessPayment.operation_id)
                .where(
                    BusinessPayment.id == payload.payment_id,
                    BusinessPayment.ledger_id == ledger_id,
                    FinancialOperation.ledger_id == ledger_id,
                )
            ).one_or_none()
            if record is None:
                raise _not_found()
            payment, operation = record
            require_current(operation, 1, payment.journal_id, "payment")
            rows = original_sources(session, ledger_id, payload)
            advance = "advance.received" if payment.direction == "receipt" else "advance.paid"
            control = (
                "receivable.customer" if payment.direction == "receipt" else "payable.supplier"
            )
            requirements = {advance: {payment.asset_id}, control: {row[2].asset_id for row in rows}}
            assets = set().union(*requirements.values())
            controls = ControlAccounts(self.engine).ensure(
                session, owner_id, ledger_id, requirements, lock_assets=assets
            )
            catalog = {
                row.asset_id: asset_definition(row)
                for row in session.scalars(
                    select(AssetRecord).where(AssetRecord.asset_id.in_(assets))
                )
            }
            try:
                payer, sources, positions = plan_sources(session, ledger_id, payment, rows, catalog)
                plan = plan_allocation_return(ledger_id, payer, payload, sources, catalog)
                groups = allocation_rows(
                    ledger_id,
                    payment.party_id,
                    plan,
                    controls,
                    {},
                    catalog,
                    description=payload.reason,
                )
                require_open_period(
                    session,
                    ledger_id,
                    SimpleNamespace(
                        transaction_date=payload.return_date,
                        recognition_date=payload.return_date,
                    ),
                )
                self._validate_balance_deltas(
                    session, ledger_id, [line for group in groups for line in group.lines], catalog
                )
            except MoneyError as error:
                raise _invalid_money(error) from None
            now = session.scalar(select(func.clock_timestamp()))
            session.execute(
                insert(BusinessAllocationReturn).values(
                    id=payload.id,
                    ledger_id=ledger_id,
                    payment_id=payment.id,
                    return_date=payload.return_date,
                    reason=payload.reason,
                    created_by=owner_id,
                    idempotency_key=key,
                    created_at=now,
                )
            )
            for item, group, requested in zip(plan.lines, groups, payload.lines, strict=True):
                persist_posting(
                    session, owner_id, ledger_id, "unallocation", plan.return_date, group, now
                )
                session.execute(
                    insert(BusinessAllocationReturnLine).values(
                        ledger_id=ledger_id,
                        return_id=payload.id,
                        payment_id=payment.id,
                        source_allocation_line_id=item.source.id,
                        source_version=requested.expected_version,
                        source_return_no=positions[item.source.id],
                        line_no=item.line_no,
                        payment_amount=item.payment_amount.to_decimal(),
                        document_amount=item.document_amount.to_decimal(),
                        operation_id=group.operation_id,
                        journal_id=group.journal_id,
                        created_at=now,
                    )
                )
            result = response_from_rows(session, payload.id)
            session.execute(
                insert(CommandReceipt).values(
                    ledger_id=ledger_id,
                    key=key,
                    hash_version=2,
                    request_hash=digest,
                    response=result.model_dump(mode="json"),
                    response_status=201,
                    operation_id=result.postings[0].operation_id,
                    created_at=now,
                )
            )
            return result
