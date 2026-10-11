"""Exact, side-effect-free payment capacity and allocation planning."""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from uuid import UUID

from coinpup_api.business.settlement_schemas import (
    BusinessAllocationCreate,
    BusinessPaymentCreate,
    PaymentDirection,
)
from coinpup_api.ledger.assets import AssetDefinition
from coinpup_api.ledger.errors import MoneyError
from coinpup_api.ledger.money import Amount


@dataclass(frozen=True, slots=True)
class PaymentFeePlan:
    account_id: UUID
    category_id: UUID
    amount: Amount


@dataclass(frozen=True, slots=True)
class PaymentPlan:
    control_key: str
    bank_amount: Amount
    advance_amount: Amount
    fees: tuple[PaymentFeePlan, ...]


@dataclass(frozen=True, slots=True)
class PaymentSource:
    id: UUID
    ledger_id: UUID
    party_id: UUID
    direction: PaymentDirection
    transaction_date: date
    amount: Amount
    applied: Amount


@dataclass(frozen=True, slots=True)
class AllocationSource:
    document_id: UUID
    document_line_id: UUID
    ledger_id: UUID
    party_id: UUID
    document_kind: str
    issue_date: date
    amount: Amount
    applied: Amount
    project_id: UUID | None


@dataclass(frozen=True, slots=True)
class AllocationLinePlan:
    line_no: int
    source: AllocationSource
    payment_amount: Amount
    document_amount: Amount
    advance_amount: Amount
    document_control_amount: Amount

    @property
    def cross_asset(self) -> bool:
        return self.payment_amount.asset != self.document_amount.asset


@dataclass(frozen=True, slots=True)
class AllocationPlan:
    advance_key: str
    document_control_key: str
    allocation_date: date
    payment_amount: Amount
    lines: tuple[AllocationLinePlan, ...]


def _integrity():
    return MoneyError("settlement_integrity", "The settlement source is inconsistent.")


def _definition(identifier, catalog):
    definition = catalog.get(identifier)
    if not isinstance(definition, AssetDefinition) or definition.asset_id != identifier:
        raise _integrity()
    return definition


def _positive(text, asset):
    amount = Amount.parse(text, asset)
    if amount.minor_units <= 0:
        raise MoneyError("amount_positive", "Settlement quantities must be positive.")
    return amount


def _remaining(total, applied, catalog):
    if not isinstance(total, Amount) or not isinstance(applied, Amount):
        raise _integrity()
    definition = _definition(total.asset.asset_id, catalog)
    if total.asset != definition or applied.asset != definition:
        raise _integrity()
    # Reconstruct to enforce the storage bound even for corrupted internal inputs.
    total = Amount(definition, total.minor_units)
    applied = Amount(definition, applied.minor_units)
    if total.minor_units <= 0 or not 0 <= applied.minor_units <= total.minor_units:
        raise _integrity()
    return total - applied


def plan_payment(
    payload: BusinessPaymentCreate, catalog: Mapping[str, AssetDefinition]
) -> PaymentPlan:
    asset = _definition(payload.asset_id, catalog)
    amount = _positive(payload.amount, asset)
    receipt = payload.direction == "receipt"
    fees = tuple(
        PaymentFeePlan(
            fee.account_id,
            fee.category_id,
            _positive(fee.amount, _definition(fee.asset_id, catalog)),
        )
        for fee in payload.fees
    )
    return PaymentPlan(
        "advance.received" if receipt else "advance.paid",
        amount if receipt else -amount,
        -amount if receipt else amount,
        fees,
    )


def plan_allocation(
    ledger_id: UUID,
    payment: PaymentSource,
    payload: BusinessAllocationCreate,
    sources: Mapping[UUID, AllocationSource],
    catalog: Mapping[str, AssetDefinition],
) -> AllocationPlan:
    if (
        payment.ledger_id != ledger_id
        or payment.id != payload.payment_id
        or payment.direction not in {"receipt", "payout"}
    ):
        raise _integrity()
    if payload.allocation_date < payment.transaction_date:
        raise MoneyError("allocation_date", "Allocation cannot precede payment or issue dates.")
    remaining = _remaining(payment.amount, payment.applied, catalog)
    receipt = payment.direction == "receipt"
    kind = "invoice" if receipt else "bill"
    allocated_units = 0
    lines = []
    for position, item in enumerate(payload.lines, 1):
        source = sources.get(item.document_line_id)
        if (
            not isinstance(source, AllocationSource)
            or source.document_id != item.document_id
            or source.document_line_id != item.document_line_id
            or source.ledger_id != ledger_id
            or source.party_id != payment.party_id
            or source.document_kind != kind
        ):
            raise _integrity()
        if payload.allocation_date < source.issue_date:
            raise MoneyError("allocation_date", "Allocation cannot precede payment or issue dates.")
        document_remaining = _remaining(source.amount, source.applied, catalog)
        payment_amount = _positive(item.payment_amount, payment.amount.asset)
        document_amount = _positive(item.document_amount, source.amount.asset)
        if payment_amount.asset == document_amount.asset and payment_amount != document_amount:
            raise MoneyError("allocation_amount_mismatch", "Same-asset quantities must match.")
        if document_amount > document_remaining:
            raise MoneyError("allocation_exceeds_document", "Allocation exceeds the open line.")
        allocated_units += payment_amount.minor_units
        if allocated_units > remaining.minor_units:
            raise MoneyError("allocation_exceeds_payment", "Allocation exceeds unapplied payment.")
        lines.append(
            AllocationLinePlan(
                position,
                source,
                payment_amount,
                document_amount,
                payment_amount if receipt else -payment_amount,
                -document_amount if receipt else document_amount,
            )
        )
    return AllocationPlan(
        "advance.received" if receipt else "advance.paid",
        "receivable.customer" if receipt else "payable.supplier",
        payload.allocation_date,
        Amount(payment.amount.asset, allocated_units),
        tuple(lines),
    )
