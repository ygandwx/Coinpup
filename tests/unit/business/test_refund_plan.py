"""Fictional refunds consume original principal without reclassifying business expenses."""

from dataclasses import FrozenInstanceError, replace
from datetime import date
from decimal import localcontext
from uuid import uuid4

import pytest
from coinpup_api.business.refund_plan import RefundReturnSource, plan_refund
from coinpup_api.business.refund_schemas import (
    BusinessRefundCreate,
    BusinessRefundResponse,
    RefundFeeReceipt,
)
from coinpup_api.business.settlement_plan import PaymentSource
from coinpup_api.ledger.assets import ASSETS
from coinpup_api.ledger.errors import MoneyError
from coinpup_api.ledger.money import Amount
from pydantic import ValidationError


def fixture(direction="payout", asset="USD", principal="20", actual="20"):
    ledger = uuid4()
    payment = PaymentSource(
        uuid4(),
        ledger,
        uuid4(),
        direction,
        date(2026, 9, 15),
        Amount.parse("100", ASSETS["USD"]),
        Amount.parse("60", ASSETS["USD"]),
    )
    source = RefundReturnSource(uuid4(), ledger, payment.id, date(2026, 10, 1))
    payload = BusinessRefundCreate(
        id=uuid4(),
        payment_id=payment.id,
        expected_version=1,
        account_id=uuid4(),
        asset_id=asset,
        amount=actual,
        payment_amount=principal,
        transaction_date="2026-10-02",
        reason="Fictional refund 中文",
        allocation_return_ids=[source.id],
    )
    return ledger, payment, Amount.parse("10", ASSETS["USD"]), payload, {source.id: source}


def run(args):
    return plan_refund(*args, ASSETS)


@pytest.mark.parametrize(
    "direction,cash,control,key",
    [
        ("receipt", "-20.00", "20.00", "advance.received"),
        ("payout", "20.00", "-20.00", "advance.paid"),
    ],
)
def test_refund_reverses_only_cash_and_original_advance(direction, cash, control, key):
    args = fixture(direction)
    original = args[3].model_dump(mode="json")
    with localcontext() as context:
        context.prec = 1
        result = run(args)
    assert result.cash_amount.to_string() == cash
    assert result.control_amount.to_string() == control
    assert result.cash_amount + result.control_amount == Amount.parse("0", ASSETS["USD"])
    assert result.payment_amount.to_string() == "20.00"
    assert result.remaining_payment_amount.to_string() == "10.00"
    assert result.control_key == key and not result.cross_asset and result.fees == ()
    assert args[3].model_dump(mode="json") == original
    with pytest.raises(FrozenInstanceError):
        result.control_key = "expense"


@pytest.mark.parametrize("direction,sign", [("receipt", -1), ("payout", 1)])
def test_cross_asset_actual_amount_is_explicit_not_original_allocation_ratio(direction, sign):
    result = run(fixture(direction, "EUR", "22", "19.37"))
    assert result.cross_asset
    assert result.cash_amount.minor_units == sign * 1937
    assert result.control_amount.minor_units == -sign * 2200
    assert result.payment_amount.asset.asset_id == "USD"
    assert result.remaining_payment_amount.to_string() == "8.00"


def test_partial_then_full_principal_and_unallocated_prepayment_need_no_return_reference():
    args = list(fixture(principal="30", actual="30"))
    assert run(args).remaining_payment_amount.minor_units == 0
    args[1] = replace(args[1], applied=Amount.parse("0", ASSETS["USD"]))
    args[2] = Amount.parse("0", ASSETS["USD"])
    args[3] = args[3].model_copy(
        update={"payment_amount": "100", "amount": "100", "allocation_return_ids": []}
    )
    args[4] = {}
    assert run(args).remaining_payment_amount.minor_units == 0


def test_explicit_fees_do_not_reduce_original_principal_or_copy_old_fees():
    args = list(fixture())
    body = args[3].model_dump(mode="json")
    body["fees"] = [
        {
            "account_id": str(uuid4()),
            "category_id": str(uuid4()),
            "asset_id": "EUR",
            "amount": "0.01",
        }
    ]
    args[3] = BusinessRefundCreate.model_validate(body)
    result = run(args)
    assert result.cash_amount.to_string() == "20.00"
    assert result.payment_amount.to_string() == "20.00"
    assert result.remaining_payment_amount.to_string() == "10.00"
    assert result.fees[0].amount.to_string() == "0.01"
    assert result.fees[0].amount.asset.asset_id == "EUR"


@pytest.mark.parametrize(
    "field,value,code",
    [
        ("payment_amount", "0", "amount_positive"),
        ("amount", "-1", "amount_positive"),
        ("payment_amount", "20.001", "amount_precision"),
        ("amount", "20.001", "amount_precision"),
        ("amount", "19.99", "refund_amount_mismatch"),
        ("transaction_date", date(2026, 9, 14), "refund_date"),
        ("transaction_date", date(2026, 9, 30), "refund_date"),
        ("asset_id", "NONEXISTENT", "settlement_integrity"),
    ],
)
def test_invalid_amounts_dates_and_definitions_fail(field, value, code):
    args = list(fixture())
    args[3] = args[3].model_copy(update={field: value})
    with pytest.raises(MoneyError) as error:
        run(args)
    assert error.value.code == code


def test_refunded_principal_cannot_be_refunded_or_allocated_twice():
    args = list(fixture(principal="30.01", actual="30.01"))
    with pytest.raises(MoneyError, match="unapplied original principal"):
        run(args)
    args[2] = Amount.parse("40.01", ASSETS["USD"])
    with pytest.raises(MoneyError) as error:
        run(args)
    assert error.value.code == "refund_integrity"


@pytest.mark.parametrize(
    "fault",
    [
        "ledger",
        "payment",
        "direction",
        "return_ledger",
        "return_payment",
        "missing_return",
        "negative_refunded",
        "wrong_refunded_asset",
    ],
)
def test_source_scope_and_corrupt_capacities_are_rejected(fault):
    args = list(fixture())
    if fault == "ledger":
        args[0] = uuid4()
    elif fault == "payment":
        args[1] = replace(args[1], id=uuid4())
    elif fault == "direction":
        args[1] = replace(args[1], direction="unknown")
    elif fault == "negative_refunded":
        args[2] = Amount.parse("-1", ASSETS["USD"])
    elif fault == "wrong_refunded_asset":
        args[2] = Amount.parse("10", ASSETS["EUR"])
    elif fault == "missing_return":
        args[4] = {}
    else:
        key, source = next(iter(args[4].items()))
        args[4] = {
            key: replace(
                source, **{"ledger_id" if fault == "return_ledger" else "payment_id": uuid4()}
            )
        }
    with pytest.raises(MoneyError) as error:
        run(args)
    assert error.value.code == "refund_integrity"


def test_maximum_and_minimum_quantities_stay_exact_in_low_decimal_context():
    args = list(fixture(principal="0.01", actual="0.01"))
    maximum = "99999999999999999999.99"
    args[1] = replace(
        args[1],
        amount=Amount.parse(maximum, ASSETS["USD"]),
        applied=Amount.parse("0", ASSETS["USD"]),
    )
    args[2] = Amount.parse("99999999999999999999.98", ASSETS["USD"])
    with localcontext() as context:
        context.prec = 1
        result = run(args)
    assert (
        result.cash_amount.to_string() == "0.01"
        and result.remaining_payment_amount.minor_units == 0
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("amount", 20),
        ("reason", " \t"),
        ("reason", "x\x00"),
        ("expected_version", True),
        ("transaction_date", "2026-02-30"),
        ("unknown", "field"),
    ],
)
def test_command_contract_rejects_coercion_or_ambiguous_input(field, value):
    raw = fixture()[3].model_dump(mode="json") | {field: value}
    with pytest.raises(ValidationError):
        BusinessRefundCreate.model_validate(raw)


def test_explicit_references_are_distinct_and_bounded():
    raw = fixture()[3].model_dump(mode="json")
    for references in ([raw["allocation_return_ids"][0]] * 2, [str(uuid4()) for _ in range(201)]):
        with pytest.raises(ValidationError):
            BusinessRefundCreate.model_validate(raw | {"allocation_return_ids": references})


def test_receipt_has_complete_cash_principal_and_separate_fee_contract():
    ledger, payment, _, body, _ = fixture()
    raw = dict(
        kind="business_refund",
        id=body.id,
        ledger_id=ledger,
        payment_id=payment.id,
        operation_id=uuid4(),
        journal_id=uuid4(),
        account_id=body.account_id,
        asset_id="USD",
        amount="20.000000000000000000",
        payment_asset_id="USD",
        payment_amount="20.000000000000000000",
        transaction_date=body.transaction_date,
        allocation_return_ids=body.allocation_return_ids,
        fees=[],
    )
    receipt = BusinessRefundResponse.model_validate(raw)
    assert receipt.payment_amount == raw["payment_amount"] and receipt.fees == ()
    for patch in (
        {"amount": "0"},
        {"payment_amount": "-20"},
        {"unknown": "field"},
        {"allocation_return_ids": body.allocation_return_ids * 2},
    ):
        with pytest.raises(ValidationError):
            BusinessRefundResponse.model_validate(raw | patch)


@pytest.mark.parametrize(
    "amount", ["0", "-0.00", "-1", "100000000000000000000", "0.0000000000000000001"]
)
def test_fee_receipts_reject_nonpositive_or_out_of_storage_range(amount):
    with pytest.raises(ValidationError):
        RefundFeeReceipt(account_id=uuid4(), asset_id="USD", amount=amount, category_id=uuid4())
