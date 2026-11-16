"""Fictional earlier revisions must preserve the meaning of every later saved return."""

from dataclasses import replace
from decimal import localcontext
from uuid import uuid4

import pytest
from coinpup_api.business.allocation_return_revision_sequence import (
    ReturnShare,
    ReturnSourceSlot,
    plan_return_source_revision,
)
from coinpup_api.ledger.assets import ASSETS
from coinpup_api.ledger.errors import MoneyError
from coinpup_api.ledger.money import Amount


def share(p, d, left="USD", right="EUR"):
    return ReturnShare(Amount(ASSETS[left], p), Amount(ASSETS[right], d))


def test_removing_early_group_invalidates_a_later_saved_final_even_with_capacity():
    first, second = uuid4(), uuid4()
    slots = (ReturnSourceSlot(1, first, share(3, 2)), ReturnSourceSlot(2, second, share(2, 2)))
    keep = plan_return_source_revision(share(5, 4), slots, first, share(3, 2))
    assert keep.source_return_no == 1 and keep.total_payment.minor_units == 5
    with pytest.raises(MoneyError) as err:
        plan_return_source_revision(share(5, 4), slots, first, None)
    assert err.value.code == "allocation_return_ratio"
    last = plan_return_source_revision(share(5, 4), slots, second, None)
    assert last.previous_payment.minor_units == last.total_payment.minor_units == 3
    assert last.previous_document.minor_units == last.total_document.minor_units == 2


def test_removed_positions_are_permanent_and_readdition_appends_regardless_of_calendar_date():
    target, other = uuid4(), uuid4()
    slots = (
        ReturnSourceSlot(1, target, None),
        ReturnSourceSlot(2, other, share(2, 2)),
        ReturnSourceSlot(3, uuid4(), None),
    )
    result = plan_return_source_revision(share(6, 6), slots, target, share(4, 4))
    assert result.source_return_no == 4 and result.previous_document.minor_units == 2
    assert result.total_payment.minor_units == 6 and slots[0].current is None
    slots += (ReturnSourceSlot(4, target, share(4, 4)),)
    changed = plan_return_source_revision(share(6, 6), slots, target, share(1, 1))
    assert changed.source_return_no == 4 and changed.total_document.minor_units == 3
    assert slots[3].current.document.minor_units == 4


@pytest.mark.parametrize(
    "fault,code",
    [
        ("wrong_prefix", "allocation_return_ratio"),
        ("one_side", "allocation_return_residual"),
        ("foreign_asset", "asset_mismatch"),
        ("missing_position", "allocation_return_integrity"),
        ("double_active", "allocation_return_integrity"),
        ("absent_target", "allocation_return_integrity"),
        ("overflow", "allocation_return_exceeds_source"),
        ("rounds_to_zero", "allocation_return_too_small"),
        ("same_asset_mismatch", "allocation_return_integrity"),
    ],
)
def test_reject_invalid_history_and_proposed_pairs(fault, code):
    original = share(5, 4)
    target, other = uuid4(), uuid4()
    slots = (ReturnSourceSlot(1, other, share(1, 1)),)
    next_share = share(3, 2)
    if fault == "wrong_prefix":
        slots = (ReturnSourceSlot(1, other, share(2, 1)),)
    if fault == "one_side":
        original = share(2, 3)
        slots = (ReturnSourceSlot(1, other, share(1, 1)),)
        next_share = share(1, 1)
    if fault == "foreign_asset":
        next_share = share(3, 2, "BTC")
    if fault == "missing_position":
        slots = (replace(slots[0], number=2),)
    if fault == "double_active":
        slots += (replace(slots[0], number=2),)
    if fault == "absent_target":
        next_share = None
    if fault == "overflow":
        next_share = share(5, 4)
    if fault == "rounds_to_zero":
        original = share(1, 5)
        slots = ()
        next_share = share(1, 1)
    if fault == "same_asset_mismatch":
        original = share(5, 4, "USD", "USD")
    with pytest.raises(MoneyError) as err:
        plan_return_source_revision(original, slots, target, next_share)
    assert err.value.code == code


def test_maximum_different_scales_and_disabled_historical_assets_use_exact_integers():
    left = replace(ASSETS["BTC"], enabled=False)
    right = replace(ASSETS["ETH"], enabled=False)
    original = ReturnShare(
        Amount(left, 10 ** (20 + left.scale) - 1), Amount(right, 10 ** (20 + right.scale) - 1)
    )
    with localcontext() as ctx:
        ctx.prec = 1
        result = plan_return_source_revision(original, (), uuid4(), original)
    assert result.total_payment == original.payment and result.total_document == original.document
