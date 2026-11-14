"""A matching digest still requires a complete, asset-exact immutable refund correction."""

from copy import deepcopy
from dataclasses import replace
from decimal import Decimal, localcontext
from uuid import uuid4

import pytest
from coinpup_api.business.refund_revision_receipts import validate_revision_intent
from coinpup_api.business.refund_revision_schemas import (
    RefundCancellationCreate,
    RefundCorrectionCreate,
    RefundRevisionResponse,
)
from coinpup_api.ledger.assets import ASSETS
from coinpup_api.ledger.money import Amount
from coinpup_api.ledger.service import LedgerError

from tests.unit.business.test_refund_revision_contract import fee, response, uid


def intent(receipt, assets=ASSETS):
    values = {"expected_version": receipt.previous_version, "reason": receipt.reason}
    if receipt.action == "cancel":
        return RefundCancellationCreate.model_validate(values)
    fact = receipt.replacement
    target = fact.model_dump(
        mode="json", exclude={"source_version", "recognition_date", "payment_asset_id"}
    )
    for field, asset_id in (("amount", fact.asset_id), ("payment_amount", fact.payment_asset_id)):
        target[field] = Amount.from_decimal(Decimal(target[field]), assets[asset_id]).to_string()
    return RefundCorrectionCreate.model_validate(values | {"replacement": target})


def replacement_receipt():
    values = response()
    values["replacement"].update(
        asset_id="EUR",
        amount="19.990000000000000000",
        payment_amount="20.000000000000000000",
        fees=[fee(amount="0.00000001"), fee(amount="0.00000002")],
        allocation_return_ids=[uid(11), uid(12)],
    )
    return RefundRevisionResponse.model_validate(values)


@pytest.mark.parametrize("action", ["correct", "cancel"])
def test_replay_preserves_raw_response_and_includes_disabled_assets_with_low_decimal_precision(
    action,
):
    original = (
        replacement_receipt()
        if action == "correct"
        else RefundRevisionResponse.model_validate(response("cancel"))
    )
    before = original.model_dump(mode="json")
    assets = {key: replace(asset, enabled=False) for key, asset in ASSETS.items()}
    with localcontext() as context:
        context.prec = 2
        command = intent(original, assets)
        assert validate_revision_intent(original, action, command, assets) is original
    assert original.model_dump(mode="json") == before


@pytest.mark.parametrize(
    "bad",
    [
        "action",
        "version",
        "reason",
        "account",
        "asset",
        "amount",
        "principal",
        "date",
        "fact_reason",
        "return_order",
        "fee_order",
        "fee_asset",
        "fee_account",
        "fee_category",
        "fee_amount",
        "fee_count",
    ],
)
def test_valid_but_different_command_cannot_be_proved_by_a_stored_refund(bad):
    original = replacement_receipt()
    payload = intent(original).model_dump(mode="json")
    target = payload["replacement"]
    if bad == "version":
        payload["expected_version"] += 1
    if bad == "reason":
        payload["reason"] = "Fictional different correction"
    if bad in {"account", "asset", "amount", "principal", "date", "fact_reason"}:
        field, value = {
            "account": ("account_id", str(uuid4())),
            "asset": ("asset_id", "USD"),
            "amount": ("amount", "19.98"),
            "principal": ("payment_amount", "20.01"),
            "date": ("transaction_date", "2031-09-09"),
            "fact_reason": ("reason", "Fictional other reason"),
        }[bad]
        target[field] = value
    if bad == "return_order":
        target["allocation_return_ids"].reverse()
    if bad == "fee_order":
        target["fees"].reverse()
    if bad == "fee_count":
        target["fees"].pop()
    if bad in {"fee_account", "fee_category", "fee_asset", "fee_amount"}:
        field, value = {
            "fee_account": ("account_id", str(uuid4())),
            "fee_category": ("category_id", str(uuid4())),
            "fee_asset": ("asset_id", "EUR"),
            "fee_amount": ("amount", "0.00000003"),
        }[bad]
        target["fees"][0][field] = value
    command = RefundCorrectionCreate.model_validate(payload)
    with pytest.raises(LedgerError) as error:
        validate_revision_intent(
            original, "cancel" if bad == "action" else "correct", command, ASSETS
        )
    assert error.value.code == "refund_revision_integrity"


@pytest.mark.parametrize(
    "bad", ["missing_old_fee_asset", "missing_cash", "wrong_identity", "sub_minimum"]
)
def test_both_sides_require_complete_actual_assets_even_for_cancel(bad):
    original = RefundRevisionResponse.model_validate(response("cancel"))
    assets = dict(ASSETS)
    if bad == "missing_old_fee_asset":
        del assets["BTC"]
    elif bad == "missing_cash":
        del assets["EUR"]
    elif bad == "wrong_identity":
        assets["USD"] = assets["EUR"]
    else:
        values = deepcopy(original.model_dump(mode="json"))
        values["previous"]["payment_amount"] = "0.001"
        original = RefundRevisionResponse.model_validate(values)
    with pytest.raises(LedgerError) as rejected:
        validate_revision_intent(original, "cancel", intent(original), assets)
    assert rejected.value.code == "refund_revision_integrity"
