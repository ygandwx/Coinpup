"""Fictional settlement quantities, original dates and immutable membership."""

from dataclasses import FrozenInstanceError, replace
from datetime import date, datetime
from decimal import localcontext
from uuid import uuid4

import pytest
from coinpup_api.business.settlement_plan import (
    AllocationSource,
    PaymentSource,
    plan_allocation,
    plan_payment,
)
from coinpup_api.business.settlement_schemas import (
    BusinessAllocationCreate,
    BusinessAllocationResponse,
    BusinessPaymentCreate,
    BusinessPaymentResponse,
)
from coinpup_api.ledger.assets import ASSETS, stablecoin_asset
from coinpup_api.ledger.errors import MoneyError
from coinpup_api.ledger.money import Amount
from pydantic import ValidationError


def payment_command():
    return BusinessPaymentCreate.model_validate(
        dict(
            id=uuid4(),
            direction="payout",
            party_id=uuid4(),
            account_id=uuid4(),
            asset_id="USD",
            amount="110.00",
            transaction_date="2026-09-15",
            description="Fictional supplier payment 中文",
        )
    )


def allocation_fixture(direction="payout", asset="EUR"):
    ledger, party = uuid4(), uuid4()
    payment = PaymentSource(
        uuid4(),
        ledger,
        party,
        direction,
        date(2026, 9, 15),
        Amount.parse("110", ASSETS["USD"]),
        Amount.parse("0", ASSETS["USD"]),
    )
    source = AllocationSource(
        uuid4(),
        uuid4(),
        ledger,
        party,
        "bill" if direction == "payout" else "invoice",
        date(2026, 9, 30),
        Amount.parse("100", ASSETS[asset]),
        Amount.parse("0", ASSETS[asset]),
        uuid4(),
    )
    command = BusinessAllocationCreate(
        id=uuid4(),
        payment_id=payment.id,
        allocation_date="2026-09-30",
        lines=[
            dict(
                document_id=source.document_id,
                document_line_id=source.document_line_id,
                payment_amount="110" if asset == "EUR" else "100",
                document_amount="100",
            )
        ],
    )
    return ledger, payment, source, command


def allocate(ledger, payment, source, command):
    return plan_allocation(ledger, payment, command, {source.document_line_id: source}, ASSETS)


@pytest.mark.parametrize(
    "direction,bank,advance,key",
    [
        ("payout", "-110.00", "110.00", "advance.paid"),
        ("receipt", "110.00", "-110.00", "advance.received"),
    ],
)
def test_payment_has_exact_opposite_principal_and_independent_fees(direction, bank, advance, key):
    raw = payment_command().model_dump()
    raw.update(
        direction=direction,
        fees=[dict(account_id=uuid4(), category_id=uuid4(), asset_id="USD", amount="1")],
    )
    command = BusinessPaymentCreate.model_validate(raw)
    with localcontext() as context:
        context.prec = 2
        plan = plan_payment(command, ASSETS)
    assert plan.control_key == key
    assert plan.bank_amount.to_string() == bank
    assert plan.advance_amount.to_string() == advance
    assert (plan.bank_amount + plan.advance_amount).minor_units == 0
    assert plan.fees[0].amount.to_string() == "1.00"
    assert Amount.parse("1000", ASSETS["USD"]) + plan.bank_amount - plan.fees[0].amount == (
        Amount.parse("889" if direction == "payout" else "1109", ASSETS["USD"])
    )
    with pytest.raises(FrozenInstanceError):
        plan.control_key = "invalid"


@pytest.mark.parametrize(
    "direction,advance,document",
    [
        ("payout", "-110.00", "100.00"),
        ("receipt", "110.00", "-100.00"),
    ],
)
def test_cross_asset_allocation_preserves_two_quantities_and_source(direction, advance, document):
    args = allocation_fixture(direction)
    with localcontext() as context:
        context.prec = 1
        plan = allocate(*args)
    line = plan.lines[0]
    assert line.cross_asset
    assert line.advance_amount.to_string() == advance
    assert line.document_control_amount.to_string() == document
    assert (line.advance_amount + -line.advance_amount).minor_units == 0
    assert (line.document_control_amount + -line.document_control_amount).minor_units == 0
    assert line.source == args[2]
    assert plan.allocation_date == date(2026, 9, 30)
    assert plan.payment_amount.to_string() == "110.00"


@pytest.mark.parametrize(
    "value,code",
    [
        ("0", "amount_positive"),
        ("-1", "amount_positive"),
        ("0.001", "amount_precision"),
        ("100000000000000000000", "amount_range"),
    ],
)
def test_invalid_payment_and_fee_amounts_are_not_rounded(value, code):
    raw = payment_command().model_dump()
    for field in ("principal", "fee"):
        candidate = dict(raw)
        if field == "principal":
            candidate["amount"] = value
        else:
            candidate["fees"] = [
                dict(account_id=uuid4(), category_id=uuid4(), asset_id="USD", amount=value)
            ]
        with pytest.raises(MoneyError) as error:
            plan_payment(BusinessPaymentCreate.model_validate(candidate), ASSETS)
        assert error.value.code == code


def test_same_asset_partial_allocation_uses_existing_capacity():
    ledger, payment, source, command = allocation_fixture(asset="USD")
    payment = replace(
        payment,
        amount=Amount.parse("300", ASSETS["USD"]),
        applied=Amount.parse("100", ASSETS["USD"]),
    )
    source = replace(
        source,
        amount=Amount.parse("300", ASSETS["USD"]),
        applied=Amount.parse("100", ASSETS["USD"]),
    )
    raw = command.model_dump()
    raw["lines"][0].update(payment_amount="50", document_amount="50.00")
    plan = allocate(ledger, payment, source, BusinessAllocationCreate.model_validate(raw))
    assert not plan.lines[0].cross_asset
    assert (payment.amount - payment.applied - plan.payment_amount).to_string() == "150.00"
    assert (source.amount - source.applied - plan.lines[0].document_amount).to_string() == "150.00"
    assert (plan.lines[0].advance_amount + plan.lines[0].document_control_amount).minor_units == 0
    raw["lines"][0]["document_amount"] = "49"
    with pytest.raises(MoneyError, match="Same-asset"):
        allocate(ledger, payment, source, BusinessAllocationCreate.model_validate(raw))


@pytest.mark.parametrize("day", ["2026-09-14", "2026-09-29"])
def test_backdating_cannot_precede_payment_or_issue_date(day):
    ledger, payment, source, command = allocation_fixture()
    raw = command.model_dump()
    raw["allocation_date"] = day
    with pytest.raises(MoneyError) as error:
        allocate(ledger, payment, source, BusinessAllocationCreate.model_validate(raw))
    assert error.value.code == "allocation_date"


@pytest.mark.parametrize("target", ["payment", "document"])
def test_allocation_cannot_spend_already_applied_capacity(target):
    ledger, payment, source, command = allocation_fixture()
    if target == "payment":
        payment = replace(payment, applied=Amount.parse("0.01", ASSETS["USD"]))
    else:
        source = replace(source, applied=Amount.parse("0.01", ASSETS["EUR"]))
    with pytest.raises(MoneyError) as error:
        allocate(ledger, payment, source, command)
    assert error.value.code == f"allocation_exceeds_{target}"


@pytest.mark.parametrize("change", ["ledger_id", "party_id", "document_id", "document_kind"])
def test_unrelated_source_cannot_be_allocated(change):
    ledger, payment, source, command = allocation_fixture()
    source = replace(source, **{change: "invoice" if change == "document_kind" else uuid4()})
    with pytest.raises(MoneyError) as error:
        allocate(ledger, payment, source, command)
    assert error.value.code == "settlement_integrity"


def test_many_documents_sum_capacity_and_preserve_explicit_order():
    ledger, payment, source, command = allocation_fixture(asset="USD")
    raw = command.model_dump()
    sources = {}
    raw["lines"] = []
    for _ in range(200):
        row = replace(source, document_id=uuid4(), document_line_id=uuid4())
        sources[row.document_line_id] = row
        raw["lines"].append(
            dict(
                document_id=row.document_id,
                document_line_id=row.document_line_id,
                payment_amount="0.50",
                document_amount="0.50",
            )
        )
    plan = plan_allocation(
        ledger, payment, BusinessAllocationCreate.model_validate(raw), sources, ASSETS
    )
    assert plan.payment_amount.to_string() == "100.00"
    assert [row.source.document_line_id for row in plan.lines] == list(sources)
    raw["lines"].append(raw["lines"][0])
    with pytest.raises(ValidationError):
        BusinessAllocationCreate.model_validate(raw)
    raw["lines"] = [raw["lines"][0], raw["lines"][0]]
    with pytest.raises(ValidationError):
        BusinessAllocationCreate.model_validate(raw)


@pytest.mark.parametrize("value", [True, 123, "2026-09-30T00:00:00Z", datetime(2026, 9, 30)])
def test_explicit_calendar_dates_reject_coercion(value):
    raw = allocation_fixture()[3].model_dump()
    raw["allocation_date"] = value
    with pytest.raises(ValidationError):
        BusinessAllocationCreate.model_validate(raw)


def test_token_identity_and_minimum_units_are_not_inferred_from_symbol():
    asset = stablecoin_asset(code="USDT", network="fictional", token_reference="token-a", scale=6)
    raw = payment_command().model_dump()
    raw.update(asset_id=asset.asset_id, amount="0.000001")
    assert (
        plan_payment(
            BusinessPaymentCreate.model_validate(raw), {asset.asset_id: asset}
        ).advance_amount.minor_units
        == 1
    )
    wrong = replace(asset, token_reference="token-b")
    with pytest.raises(MoneyError):
        plan_payment(BusinessPaymentCreate.model_validate(raw), {asset.asset_id: wrong})


def test_receipts_require_complete_distinct_ordered_membership_and_matching_parent():
    ledger, payment, source, command = allocation_fixture()
    member = dict(
        line_no=1,
        document_id=source.document_id,
        document_line_id=source.document_line_id,
        operation_id=uuid4(),
        journal_id=uuid4(),
    )
    raw = dict(
        kind="business_allocation",
        id=command.id,
        ledger_id=ledger,
        payment_id=payment.id,
        postings=[member],
    )
    response = BusinessAllocationResponse.model_validate(raw)
    parent = dict(
        kind="business_payment",
        id=payment.id,
        ledger_id=ledger,
        operation_id=uuid4(),
        journal_id=uuid4(),
        initial_allocation=response,
    )
    assert BusinessPaymentResponse.model_validate(parent).initial_allocation == response
    for broken in (
        [{**member, "line_no": True}],
        [{**member, "line_no": 2}],
        [member, {**member, "line_no": 2}],
        [],
    ):
        with pytest.raises(ValidationError):
            BusinessAllocationResponse.model_validate({**raw, "postings": broken})
    for change in (
        {"ledger_id": uuid4()},
        {"id": uuid4()},
        {"operation_id": member["operation_id"]},
        {"journal_id": member["journal_id"]},
    ):
        with pytest.raises(ValidationError):
            BusinessPaymentResponse.model_validate({**parent, **change})


def test_v2_hash_keeps_original_amount_text_presence_scope_and_member_order():
    import json

    from coinpup_api.business.settlement_hash import allocation_hash_v2, payment_hash_v2

    ledger, payment, source, allocation = allocation_fixture()
    command = payment_command()
    raw = command.model_dump(mode="json", exclude_unset=True)
    digest = payment_hash_v2(ledger, command)
    assert digest == payment_hash_v2(ledger, command, raw_body=json.dumps(raw).encode())
    assert digest == payment_hash_v2(
        ledger, command, raw_body=json.dumps(raw, sort_keys=True).encode()
    )
    for body in (
        {**raw, "amount": "110.0"},
        {**raw, "fees": []},
        {**raw, "initial_allocation": None},
    ):
        assert digest != payment_hash_v2(ledger, command, raw_body=json.dumps(body).encode())
    assert digest != payment_hash_v2(uuid4(), command)
    assert digest != allocation_hash_v2(ledger, command, raw_body=json.dumps(raw).encode())
    raw_allocation = allocation.model_dump(mode="json")
    second = dict(
        raw_allocation["lines"][0], document_id=str(uuid4()), document_line_id=str(uuid4())
    )
    raw_allocation["lines"].append(second)
    original = allocation_hash_v2(ledger, allocation, raw_body=json.dumps(raw_allocation).encode())
    raw_allocation["lines"].reverse()
    assert original != allocation_hash_v2(
        ledger, allocation, raw_body=json.dumps(raw_allocation).encode()
    )
    raw_allocation["payment_id"] = str(uuid4())
    assert original != allocation_hash_v2(
        ledger, allocation, raw_body=json.dumps(raw_allocation).encode()
    )


@pytest.mark.parametrize("quantity", ["0", "-1", "0.001"])
def test_allocation_rejects_invalid_quantities_on_both_sides(quantity):
    ledger, payment, source, command = allocation_fixture()
    for field in ("payment_amount", "document_amount"):
        raw = command.model_dump()
        raw["lines"][0][field] = quantity
        with pytest.raises(MoneyError):
            allocate(ledger, payment, source, BusinessAllocationCreate.model_validate(raw))


@pytest.mark.parametrize(
    "field,value",
    [
        ("amount", 1.5),
        ("amount", True),
        ("amount", "1e2"),
        ("description", "Fictional\x00"),
        ("recognition_date", "2026-09-15"),
    ],
)
def test_payment_command_does_not_coerce_or_accept_unsupported_fields(field, value):
    raw = payment_command().model_dump()
    with pytest.raises(ValidationError):
        BusinessPaymentCreate.model_validate({**raw, field: value})


def test_payment_requires_explicit_identity_and_bounded_initial_group_and_fees():
    raw = payment_command().model_dump(mode="json")
    _, _, _, allocation = allocation_fixture()
    initial = allocation.model_dump(mode="json", exclude={"payment_id"})
    for candidate in (
        {key: value for key, value in raw.items() if key != "id"},
        {**raw, "id": None},
        {**raw, "initial_allocation": {**initial, "lines": []}},
        {
            **raw,
            "initial_allocation": {key: value for key, value in initial.items() if key != "id"},
        },
        {
            **raw,
            "initial_allocation": {
                key: value for key, value in initial.items() if key != "allocation_date"
            },
        },
    ):
        with pytest.raises(ValidationError):
            BusinessPaymentCreate.model_validate(candidate)
    fee = dict(account_id=str(uuid4()), category_id=str(uuid4()), asset_id="USD", amount="0.01")
    accepted = BusinessPaymentCreate.model_validate({**raw, "fees": [fee] * 20})
    assert len(plan_payment(accepted, ASSETS).fees) == 20
    with pytest.raises(ValidationError):
        BusinessPaymentCreate.model_validate({**raw, "fees": [fee] * 21})


@pytest.mark.parametrize(
    "asset,text",
    [
        ("USD", "0.01"),
        ("USD", "99999999999999999999.99"),
        ("BTC", "0.00000001"),
        ("ETH", "0.000000000000000001"),
    ],
)
def test_payment_supports_exact_minimum_units_and_storage_maximum(asset, text):
    raw = payment_command().model_dump()
    raw.update(asset_id=asset, amount=text)
    with localcontext() as context:
        context.prec = 1
        plan = plan_payment(BusinessPaymentCreate.model_validate(raw), ASSETS)
    assert plan.advance_amount.to_string() == text
    assert plan.bank_amount == -plan.advance_amount
