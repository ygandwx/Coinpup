"""Exact refund principal capacity and signed cash/control plans without persistence."""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from uuid import UUID

from coinpup_api.business.refund_schemas import BusinessRefundCreate
from coinpup_api.business.settlement_plan import (
    PaymentFeePlan,
    PaymentSource,
    _definition,
    _positive,
    _remaining,
)
from coinpup_api.ledger.assets import AssetDefinition
from coinpup_api.ledger.errors import MoneyError
from coinpup_api.ledger.money import Amount


@dataclass(frozen=True, slots=True)
class RefundReturnSource:
    id: UUID
    ledger_id: UUID
    payment_id: UUID
    return_date: date


@dataclass(frozen=True, slots=True)
class RefundPlan:
    control_key: str
    cash_amount: Amount
    control_amount: Amount
    payment_amount: Amount
    remaining_payment_amount: Amount
    fees: tuple[PaymentFeePlan, ...]

    @property
    def cross_asset(self):
        return self.cash_amount.asset != self.payment_amount.asset


def plan_refund(
    ledger_id: UUID,
    payment: PaymentSource,
    refunded: Amount,
    payload: BusinessRefundCreate,
    returns: Mapping[UUID, RefundReturnSource],
    catalog: Mapping[str, AssetDefinition],
) -> RefundPlan:
    if (
        payment.ledger_id != ledger_id
        or payment.id != payload.payment_id
        or payment.direction not in {"receipt", "payout"}
    ):
        raise MoneyError("refund_integrity", "The original payment scope is inconsistent.")
    remaining = _remaining(payment.amount, payment.applied, catalog)
    if (
        not isinstance(refunded, Amount)
        or refunded.asset != remaining.asset
        or refunded.minor_units < 0
        or refunded > remaining
    ):
        raise MoneyError("refund_integrity", "The original principal capacity is inconsistent.")
    remaining = remaining - refunded
    if payload.transaction_date < payment.transaction_date:
        raise MoneyError("refund_date", "Refund cannot precede the original payment or returns.")
    for identifier in payload.allocation_return_ids:
        source = returns.get(identifier)
        if (
            not isinstance(source, RefundReturnSource)
            or source.id != identifier
            or source.ledger_id != ledger_id
            or source.payment_id != payment.id
        ):
            raise MoneyError("refund_integrity", "The explicit return source is inconsistent.")
        if payload.transaction_date < source.return_date:
            raise MoneyError(
                "refund_date", "Refund cannot precede the original payment or returns."
            )
    principal = _positive(payload.payment_amount, remaining.asset)
    cash = _positive(payload.amount, _definition(payload.asset_id, catalog))
    if principal.asset == cash.asset and principal != cash:
        raise MoneyError("refund_amount_mismatch", "Same-asset refund quantities must match.")
    if principal > remaining:
        raise MoneyError("refund_exceeds_payment", "Refund exceeds unapplied original principal.")
    fees = tuple(
        PaymentFeePlan(
            fee.account_id,
            fee.category_id,
            _positive(fee.amount, _definition(fee.asset_id, catalog)),
        )
        for fee in payload.fees
    )
    receipt = payment.direction == "receipt"
    return RefundPlan(
        "advance.received" if receipt else "advance.paid",
        -cash if receipt else cash,
        principal if receipt else -principal,
        principal,
        remaining - principal,
        fees,
    )
