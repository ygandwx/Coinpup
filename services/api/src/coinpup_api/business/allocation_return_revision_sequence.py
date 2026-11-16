"""Replay a single original mapping in permanent posting order after a proposed group revision."""

from dataclasses import dataclass
from uuid import UUID

from coinpup_api.business.allocation_return_plan import remaining_return_pair, validate_return_pair
from coinpup_api.ledger.errors import MoneyError
from coinpup_api.ledger.money import Amount


@dataclass(frozen=True, slots=True)
class ReturnShare:
    payment: Amount
    document: Amount

    def __post_init__(self):
        if self.payment.minor_units <= 0 or self.document.minor_units <= 0:
            raise MoneyError("amount_positive", "Both return quantities must be positive.")


@dataclass(frozen=True, slots=True)
class ReturnSourceSlot:
    """Previous positions remain present after removal; re-addition gets a new position."""

    number: int
    return_id: UUID
    current: ReturnShare | None


@dataclass(frozen=True, slots=True)
class ReturnSequencePlan:
    source_return_no: int
    previous_payment: Amount
    previous_document: Amount
    total_payment: Amount
    total_document: Amount


def plan_return_source_revision(
    original: ReturnShare,
    slots: tuple[ReturnSourceSlot, ...],
    return_id: UUID,
    replacement: ReturnShare | None,
) -> ReturnSequencePlan:
    """Prove the changed slot and every later payment/document pair with their actual assets."""
    p, d = original.payment, original.document
    if p.asset == d.asset and p != d:
        raise MoneyError("allocation_return_integrity", "Original same-asset sides disagree.")
    total_p, total_d = Amount(p.asset, 0), Amount(d.asset, 0)
    active = set()
    target = None
    for position, slot in enumerate(slots, 1):
        if type(slot.number) is not int or slot.number != position:
            raise MoneyError("allocation_return_integrity", "The permanent source order has a gap.")
        if slot.current is None:
            continue
        if slot.return_id in active:
            raise MoneyError("allocation_return_integrity", "A group has two active source slots.")
        active.add(slot.return_id)
        share = slot.current
        if slot.return_id == return_id:
            target = (position, total_p, total_d)
            share = replacement
        if share is not None:
            remaining_p, remaining_d = remaining_return_pair(p, d, total_p, total_d)
            validate_return_pair(p, d, remaining_p, remaining_d, share.payment, share.document)
            total_p, total_d = total_p + share.payment, total_d + share.document
    if target is None:
        if replacement is None:
            raise MoneyError("allocation_return_integrity", "No current source slot to remove.")
        target = (len(slots) + 1, total_p, total_d)
        remaining_p, remaining_d = remaining_return_pair(p, d, total_p, total_d)
        validate_return_pair(
            p, d, remaining_p, remaining_d, replacement.payment, replacement.document
        )
        total_p, total_d = total_p + replacement.payment, total_d + replacement.document
    return ReturnSequencePlan(*target, total_p, total_d)
