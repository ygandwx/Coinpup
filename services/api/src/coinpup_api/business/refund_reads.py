"""Scoped snapshot reads; a remaining quantity never substitutes for an original receipt."""

from decimal import Decimal

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import aliased

from coinpup_api.business.drafts import _fields
from coinpup_api.business.refund_models import BusinessRefund
from coinpup_api.business.refund_read_schemas import (
    RefundCapacity,
    RefundCurrent,
    RefundDetail,
    RefundSummary,
)
from coinpup_api.business.refund_receipts import integrity, response_from_rows
from coinpup_api.business.refund_revision_current import read_current_refund
from coinpup_api.business.refund_revision_receipts import integrity as revision_integrity
from coinpup_api.business.refund_revision_schemas import refund_fact_amounts
from coinpup_api.business.refund_schemas import BusinessRefundResponse
from coinpup_api.business.settlement_balances import quantities
from coinpup_api.business.settlement_models import BusinessPayment
from coinpup_api.business.settlement_totals import payment_totals
from coinpup_api.ledger.common import asset_definition
from coinpup_api.ledger.errors import MoneyError
from coinpup_api.ledger.models import AssetRecord, CommandReceipt, FinancialOperation
from coinpup_api.ledger.money import Amount
from coinpup_api.ledger.service import LedgerService, _not_found, _page


def summaries(session, ledger_id, *, identifier=None, payment_id=None, limit=100, offset=0):
    principal = aliased(AssetRecord)
    statement = (
        select(BusinessRefund, BusinessPayment, AssetRecord, principal)
        .select_from(BusinessRefund)
        .join(BusinessPayment, BusinessPayment.id == BusinessRefund.payment_id)
        .join(AssetRecord, AssetRecord.asset_id == BusinessRefund.asset_id)
        .join(principal, principal.asset_id == BusinessPayment.asset_id)
        .where(BusinessRefund.ledger_id == ledger_id, BusinessPayment.ledger_id == ledger_id)
        .order_by(BusinessRefund.created_at, BusinessRefund.id)
        .limit(limit)
        .offset(offset)
    )
    if identifier is not None:
        statement = statement.where(BusinessRefund.id == identifier)
    if payment_id is not None:
        statement = statement.where(BusinessRefund.payment_id == payment_id)
    result = []
    try:
        for row, payment, cash, original in session.execute(statement):
            amount = Amount.from_decimal(row.amount, asset_definition(cash))
            paid = Amount.from_decimal(row.payment_amount, asset_definition(original))
            if min(amount.minor_units, paid.minor_units) <= 0:
                raise integrity()
            result.append(
                (
                    row,
                    RefundSummary(
                        **_fields(
                            row,
                            RefundSummary,
                            party_id=payment.party_id,
                            payment_direction=payment.direction,
                            payment_asset_id=payment.asset_id,
                            amount=amount.to_string(),
                            payment_amount=paid.to_string(),
                        )
                    ),
                )
            )
    except (ValidationError, MoneyError):
        raise integrity() from None
    return result


class BusinessRefundReads(LedgerService):
    def list_refunds(self, owner_id, ledger_id, *, payment_id=None, limit=100, offset=0):
        _page(limit, offset)
        with self._transaction(owner_id, read_only=True) as session:
            self._ledger(session, owner_id, ledger_id)
            return [
                summary
                for _, summary in summaries(
                    session, ledger_id, payment_id=payment_id, limit=limit, offset=offset
                )
            ]

    def get_refund(self, owner_id, ledger_id, refund_id):
        with self._transaction(owner_id, read_only=True) as session:
            self._ledger(session, owner_id, ledger_id)
            rows = summaries(session, ledger_id, identifier=refund_id)
            if not rows:
                raise _not_found()
            saved, summary = rows[0]
            expected = response_from_rows(session, saved.id)
            receipt = session.get(CommandReceipt, (ledger_id, saved.idempotency_key))
            if (
                receipt is None
                or receipt.hash_version != 2
                or receipt.response_status != 201
                or receipt.operation_id != expected.operation_id
            ):
                raise integrity()
            try:
                original = BusinessRefundResponse.model_validate(receipt.response)
                if original != expected:
                    raise integrity()
                return RefundDetail(
                    **summary.model_dump(), source_version=saved.source_version, receipt=original
                )
            except ValidationError:
                raise integrity() from None

    def current(self, owner_id, ledger_id, refund_id):
        with self._transaction(owner_id, read_only=True) as session:
            self._ledger(session, owner_id, ledger_id)
            state = read_current_refund(session, ledger_id, refund_id)
            fact = state.fact
            asset_ids = {fact.asset_id, fact.payment_asset_id, *(fee.asset_id for fee in fact.fees)}
            records = session.scalars(
                select(AssetRecord).where(AssetRecord.asset_id.in_(asset_ids))
            )
            assets = {record.asset_id: asset_definition(record) for record in records}
            try:
                cash, principal, fees = refund_fact_amounts(fact, assets)
                active = state.operation.status == "active"
                current_fact = (
                    fact.model_copy(
                        update={
                            "amount": cash.to_string(),
                            "payment_amount": principal.to_string(),
                            "fees": tuple(
                                fee.model_copy(update={"amount": amount.to_string()})
                                for fee, amount in zip(fact.fees, fees, strict=True)
                            ),
                        }
                    )
                    if active
                    else None
                )
                return RefundCurrent(
                    refund_id=state.refund.id,
                    ledger_id=ledger_id,
                    payment_id=state.payment.id,
                    operation_id=state.operation.id,
                    party_id=state.payment.party_id,
                    payment_direction=state.payment.direction,
                    payment_asset_id=state.payment.asset_id,
                    version=state.operation.version,
                    status=state.operation.status,
                    revision_id=state.revision.id if state.revision else None,
                    revision_reason=state.revision.revision_reason if state.revision else None,
                    journal_id=state.journal.id if active else None,
                    fact=current_fact,
                )
            except (MoneyError, ValidationError):
                raise revision_integrity() from None

    def capacity(self, owner_id, ledger_id, payment_id):
        with self._transaction(owner_id, read_only=True) as session:
            self._ledger(session, owner_id, ledger_id)
            saved = session.execute(
                select(BusinessPayment, AssetRecord, FinancialOperation)
                .select_from(BusinessPayment)
                .join(AssetRecord, AssetRecord.asset_id == BusinessPayment.asset_id)
                .join(FinancialOperation, FinancialOperation.id == BusinessPayment.operation_id)
                .where(
                    BusinessPayment.id == payment_id,
                    BusinessPayment.ledger_id == ledger_id,
                    FinancialOperation.ledger_id == ledger_id,
                )
            ).one_or_none()
            if saved is None:
                raise _not_found()
            payment, asset, operation = saved
            if operation.kind != "payment":
                raise integrity()
            applied, refunded = payment_totals(session, ledger_id, {payment_id}).get(
                payment_id, (Decimal(0), Decimal(0))
            )
            try:
                definition = asset_definition(asset)
                total, used, remaining = quantities(
                    payment.amount, applied, definition, refunded=refunded
                )
                return RefundCapacity(
                    id=payment.id,
                    ledger_id=ledger_id,
                    party_id=payment.party_id,
                    payment_direction=payment.direction,
                    asset_id=asset.asset_id,
                    transaction_date=payment.transaction_date,
                    expected_version=operation.version,
                    source_status=operation.status,
                    amount=total,
                    applied_amount=used,
                    refunded_amount=Amount.from_decimal(refunded, definition).to_string(),
                    refundable_amount=remaining,
                )
            except (ValidationError, MoneyError):
                raise integrity() from None
