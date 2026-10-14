"""Side-effect-free original-ratio return planning in integer minimum units."""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from uuid import UUID

from coinpup_api.business.allocation_return_schemas import BusinessAllocationReturnCreate
from coinpup_api.business.settlement_plan import PaymentSource
from coinpup_api.ledger.assets import AssetDefinition
from coinpup_api.ledger.errors import MoneyError
from coinpup_api.ledger.money import Amount


@dataclass(frozen=True, slots=True)
class AllocationReturnSource:
    id: UUID
    ledger_id: UUID
    payment_id: UUID
    party_id: UUID
    operation_id: UUID
    allocation_date: date
    document_id: UUID
    document_line_id: UUID
    document_kind: str
    issue_date: date
    project_id: UUID | None
    payment_amount: Amount
    document_amount: Amount
    returned_payment: Amount
    returned_document: Amount


@dataclass(frozen=True, slots=True)
class AllocationReturnLinePlan:
    line_no: int
    source: AllocationReturnSource
    payment_amount: Amount
    document_amount: Amount
    advance_amount: Amount
    document_control_amount: Amount

    @property
    def cross_asset(self) -> bool:
        return self.payment_amount.asset != self.document_amount.asset


@dataclass(frozen=True, slots=True)
class AllocationReturnPlan:
    advance_key: str
    document_control_key: str
    return_date: date
    payment_amount: Amount
    lines: tuple[AllocationReturnLinePlan, ...]


def _integrity():
    return MoneyError("allocation_return_integrity", "The original allocation is inconsistent.")


def _quantities(total, returned, catalog):
    if not isinstance(total, Amount) or not isinstance(returned, Amount):
        raise _integrity()
    asset = catalog.get(total.asset.asset_id)
    if not isinstance(asset, AssetDefinition) or asset != total.asset or returned.asset != asset:
        raise _integrity()
    total, returned = Amount(asset, total.minor_units), Amount(asset, returned.minor_units)
    if total.minor_units <= 0 or not 0 <= returned.minor_units <= total.minor_units:
        raise _integrity()
    return total, returned


def _positive(text, asset):
    amount = Amount.parse(text, asset)
    if amount.minor_units <= 0:
        raise MoneyError("amount_positive", "Return quantities must be positive.")
    return amount


def _return_amounts(source, item, catalog):
    original_p, returned_p = _quantities(source.payment_amount, source.returned_payment, catalog)
    original_d, returned_d = _quantities(source.document_amount, source.returned_document, catalog)
    remaining_p, remaining_d = original_p - returned_p, original_d - returned_d
    same_asset = original_p.asset == original_d.asset
    if (remaining_p.minor_units == 0) != (remaining_d.minor_units == 0) or (
        same_asset and (original_p != original_d or returned_p != returned_d)
    ):
        raise _integrity()
    payment = _positive(item.payment_amount, original_p.asset)
    document = _positive(item.document_amount, original_d.asset)
    if payment > remaining_p or document > remaining_d:
        raise MoneyError("allocation_return_exceeds_source", "Return exceeds the original mapping.")
    if document == remaining_d:
        expected_units = remaining_p.minor_units
    elif same_asset:
        expected_units = document.minor_units
    else:
        # Positive HALF_UP using the original pair, independent of Decimal precision.
        expected_units = (
            2 * document.minor_units * original_p.minor_units + original_d.minor_units
        ) // (2 * original_d.minor_units)
    if expected_units == 0:
        raise MoneyError("allocation_return_too_small", "Choose a larger or full remaining return.")
    if payment.minor_units != expected_units:
        raise MoneyError(
            "allocation_return_ratio", "Payment quantity must follow the original ratio."
        )
    if (payment == remaining_p) != (document == remaining_d):
        raise MoneyError(
            "allocation_return_residual", "A partial return cannot leave only one side."
        )
    return payment, document


def plan_allocation_return(
    ledger_id: UUID,
    payment: PaymentSource,
    payload: BusinessAllocationReturnCreate,
    sources: Mapping[UUID, AllocationReturnSource],
    catalog: Mapping[str, AssetDefinition],
) -> AllocationReturnPlan:
    if (
        payment.ledger_id != ledger_id
        or payment.id != payload.payment_id
        or payment.direction not in {"receipt", "payout"}
    ):
        raise _integrity()
    principal, applied = _quantities(payment.amount, payment.applied, catalog)
    receipt = payment.direction == "receipt"
    kind = "invoice" if receipt else "bill"
    total_units, lines = 0, []
    for position, item in enumerate(payload.lines, 1):
        source = sources.get(item.source_allocation_line_id)
        if (
            not isinstance(source, AllocationReturnSource)
            or source.id != item.source_allocation_line_id
            or source.ledger_id != ledger_id
            or source.payment_id != payment.id
            or source.party_id != payment.party_id
            or source.document_kind != kind
            or not isinstance(source.payment_amount, Amount)
            or source.payment_amount.asset != principal.asset
            or source.allocation_date < max(payment.transaction_date, source.issue_date)
        ):
            raise _integrity()
        if payload.return_date < source.allocation_date:
            raise MoneyError(
                "allocation_return_date", "Return cannot precede the original application."
            )
        payment_amount, document_amount = _return_amounts(source, item, catalog)
        total_units += payment_amount.minor_units
        if total_units > applied.minor_units:
            raise _integrity()
        lines.append(
            AllocationReturnLinePlan(
                position,
                source,
                payment_amount,
                document_amount,
                -payment_amount if receipt else payment_amount,
                document_amount if receipt else -document_amount,
            )
        )
    return AllocationReturnPlan(
        "advance.received" if receipt else "advance.paid",
        "receivable.customer" if receipt else "payable.supplier",
        payload.return_date,
        Amount(principal.asset, total_units),
        tuple(lines),
    )
