"""Atomic actual refunds, independent expenses and permanent original-intent replay."""

from decimal import Decimal
from types import SimpleNamespace

from sqlalchemy import func, insert, select

from coinpup_api.business.allocation_return_models import BusinessAllocationReturn
from coinpup_api.business.allocation_return_sources import require_current
from coinpup_api.business.refund_models import BusinessRefund, BusinessRefundReturn
from coinpup_api.business.refund_plan import RefundReturnSource, plan_refund
from coinpup_api.business.refund_receipts import replay_refund, response_from_rows
from coinpup_api.business.refund_rows import refund_rows
from coinpup_api.business.settlement_models import BusinessPayment
from coinpup_api.business.settlement_plan import PaymentSource
from coinpup_api.business.settlement_rows import persist_posting
from coinpup_api.business.settlement_totals import payment_totals
from coinpup_api.ledger.balances import BalanceQueries
from coinpup_api.ledger.commands.fees import FeeCommands
from coinpup_api.ledger.common import PostingCore, _invalid_money, asset_definition
from coinpup_api.ledger.control_accounts import ControlAccounts
from coinpup_api.ledger.errors import MoneyError
from coinpup_api.ledger.idempotency import _IDEMPOTENCY_KEY, _v2_hash
from coinpup_api.ledger.models import AssetRecord, CommandReceipt, FinancialOperation
from coinpup_api.ledger.money import Amount
from coinpup_api.ledger.period_checks import require_open_period
from coinpup_api.ledger.service import LedgerError, _not_found


class BusinessRefundService(FeeCommands, BalanceQueries, PostingCore):
    def create(self, owner_id, ledger_id, payload, key, *, raw_body=None):
        if not isinstance(key, str) or _IDEMPOTENCY_KEY.fullmatch(key) is None:
            raise LedgerError("invalid_idempotency_key", 422, "Use 1–128 visible ASCII characters.")
        with self._transaction(owner_id, write=True, read_committed=True) as session:
            _, entity = self._locked_ledger(session, owner_id, ledger_id)
            digest = _v2_hash("business_refund", ledger_id, payload, raw_body)
            receipt = session.get(CommandReceipt, (ledger_id, key))
            if receipt is not None:
                return replay_refund(session, receipt, ledger_id, payload, digest)
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
            require_current(operation, payload.expected_version, payment.journal_id, "payment")
            references = session.scalars(
                select(BusinessAllocationReturn).where(
                    BusinessAllocationReturn.ledger_id == ledger_id,
                    BusinessAllocationReturn.payment_id == payment.id,
                    BusinessAllocationReturn.id.in_(payload.allocation_return_ids),
                )
            ).all()
            if len(references) != len(payload.allocation_return_ids):
                raise _not_found()
            advance = "advance.received" if payment.direction == "receipt" else "advance.paid"
            assets = {payment.asset_id, payload.asset_id} | {f.asset_id for f in payload.fees}
            controls = ControlAccounts(self.engine).ensure(
                session, owner_id, ledger_id, {advance: {payment.asset_id}}, lock_assets=assets
            )
            catalog = {
                row.asset_id: asset_definition(row)
                for row in session.scalars(
                    select(AssetRecord).where(AssetRecord.asset_id.in_(assets))
                )
            }
            self._account(session, ledger_id, payload.account_id, payload.asset_id)
            try:
                asset = catalog[payment.asset_id]
                applied, refunded = payment_totals(session, ledger_id, {payment.id}).get(
                    payment.id, (Decimal(0), Decimal(0))
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
                returns = {
                    row.id: RefundReturnSource(row.id, ledger_id, payment.id, row.return_date)
                    for row in references
                }
                plan = plan_refund(
                    ledger_id,
                    source,
                    Amount.from_decimal(refunded, asset),
                    payload,
                    returns,
                    catalog,
                )
                group = refund_rows(
                    self, session, ledger_id, payment.party_id, payload, plan, controls, catalog
                )
                require_open_period(
                    session,
                    ledger_id,
                    SimpleNamespace(
                        transaction_date=payload.transaction_date,
                        recognition_date=payload.transaction_date,
                    ),
                )
                self._validate_balance_deltas(session, ledger_id, group.lines, catalog)
            except MoneyError as error:
                raise _invalid_money(error) from None
            now = session.scalar(select(func.clock_timestamp()))
            session.execute(
                insert(BusinessRefund).values(
                    id=payload.id,
                    ledger_id=ledger_id,
                    payment_id=payment.id,
                    source_version=payload.expected_version,
                    account_id=payload.account_id,
                    asset_id=payload.asset_id,
                    amount=Amount.parse(payload.amount, catalog[payload.asset_id]).to_decimal(),
                    payment_amount=plan.payment_amount.to_decimal(),
                    transaction_date=payload.transaction_date,
                    reason=payload.reason,
                    fees=[f.model_dump(mode="json") for f in payload.fees],
                    created_by=owner_id,
                    idempotency_key=key,
                    operation_id=group.operation_id,
                    journal_id=group.journal_id,
                    created_at=now,
                )
            )
            for position, identifier in enumerate(payload.allocation_return_ids, 1):
                session.execute(
                    insert(BusinessRefundReturn).values(
                        refund_id=payload.id,
                        payment_id=payment.id,
                        ledger_id=ledger_id,
                        return_id=identifier,
                        line_no=position,
                        created_at=now,
                    )
                )
            persist_posting(
                session, owner_id, ledger_id, "refund", payload.transaction_date, group, now
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
                    operation_id=result.operation_id,
                    created_at=now,
                )
            )
            return result
