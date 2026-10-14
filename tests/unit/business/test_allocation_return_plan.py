"""Fictional original proportions, rounding residuals and immutable membership."""

from dataclasses import FrozenInstanceError, replace
from datetime import date, datetime
from decimal import localcontext
from uuid import uuid4

import pytest
from coinpup_api.business.allocation_return_plan import (
    AllocationReturnSource,
    plan_allocation_return,
)
from coinpup_api.business.allocation_return_schemas import (
    BusinessAllocationReturnCreate,
    BusinessAllocationReturnResponse,
)
from coinpup_api.business.settlement_plan import PaymentSource
from coinpup_api.ledger.assets import ASSETS, fiat_asset, stablecoin_asset
from coinpup_api.ledger.errors import MoneyError
from coinpup_api.ledger.money import Amount
from pydantic import ValidationError


def fixture(
    *,
    direction="payout",
    asset="EUR",
    original_p="110",
    original_d="100",
    returned_p="0",
    returned_d="0",
    requested_p="22",
    requested_d="20",
    catalog=ASSETS,
):
    ledger, party = uuid4(), uuid4()
    p, d = catalog["USD"], catalog[asset]
    payment = PaymentSource(
        uuid4(),
        ledger,
        party,
        direction,
        date(2026, 9, 15),
        Amount.parse(original_p, p),
        Amount.parse(original_p, p) - Amount.parse(returned_p, p),
    )
    source = AllocationReturnSource(
        uuid4(),
        ledger,
        payment.id,
        party,
        uuid4(),
        date(2026, 9, 30),
        uuid4(),
        uuid4(),
        "invoice" if direction == "receipt" else "bill",
        date(2026, 9, 30),
        uuid4(),
        Amount.parse(original_p, p),
        Amount.parse(original_d, d),
        Amount.parse(returned_p, p),
        Amount.parse(returned_d, d),
    )
    command = BusinessAllocationReturnCreate(
        id=uuid4(),
        payment_id=payment.id,
        return_date="2026-10-01",
        reason="Fictional return 中文",
        lines=[
            dict(
                source_allocation_line_id=source.id,
                expected_version=1,
                payment_amount=requested_p,
                document_amount=requested_d,
            )
        ],
    )
    return ledger, payment, source, command


def plan(args, catalog=ASSETS):
    ledger, payment, source, command = args
    return plan_allocation_return(ledger, payment, command, {source.id: source}, catalog)


@pytest.mark.parametrize(
    "direction,advance,document",
    [
        ("payout", "22.00", "-20.00"),
        ("receipt", "-22.00", "20.00"),
    ],
)
def test_partial_returns_restore_advances_without_a_cash_or_expense_posting(
    direction, advance, document
):
    args = fixture(direction=direction)
    with localcontext() as context:
        context.prec = 1
        result = plan(args)
    line = result.lines[0]
    assert line.payment_amount.to_string() == "22.00"
    assert line.document_amount.to_string() == "20.00"
    assert line.advance_amount.to_string() == advance
    assert line.document_control_amount.to_string() == document
    assert line.cross_asset and line.source == args[2]
    assert result.advance_key == ("advance.received" if direction == "receipt" else "advance.paid")
    assert result.document_control_key == (
        "receivable.customer" if direction == "receipt" else "payable.supplier"
    )
    # Each original asset has its own balancing exchange leg; they never offset each other.
    assert (line.advance_amount + -line.advance_amount).minor_units == 0
    assert (line.document_control_amount + -line.document_control_amount).minor_units == 0
    assert (args[1].applied - result.payment_amount).to_string() == "88.00"
    with pytest.raises(FrozenInstanceError):
        result.payment_amount = Amount.parse("1", ASSETS["USD"])


def test_repeated_returns_use_original_ratio_and_only_the_final_return_absorbs_residual():
    returned_p, returned_d = 0, 0
    observed = []
    for expected in (33, 33, 34):
        args = fixture(
            original_p="1",
            original_d="3",
            returned_p=Amount(ASSETS["USD"], returned_p).to_string(),
            returned_d=Amount(ASSETS["EUR"], returned_d).to_string(),
            requested_p=Amount(ASSETS["USD"], expected).to_string(),
            requested_d="1",
        )
        line = plan(args).lines[0]
        observed.append(line.payment_amount.minor_units)
        returned_p += line.payment_amount.minor_units
        returned_d += line.document_amount.minor_units
    assert observed == [33, 33, 34]
    assert returned_p == 100 and returned_d == 300
    # Cumulative rounding or recalculating the remaining ratio would allocate the residual early.
    with pytest.raises(MoneyError) as error:
        plan(
            fixture(
                original_p="1",
                original_d="3",
                returned_p="0.33",
                returned_d="1",
                requested_p="0.34",
                requested_d="1",
            )
        )
    assert error.value.code == "allocation_return_ratio"


def test_exact_half_unit_rounds_up_and_final_quantity_is_the_actual_remaining_pair():
    first = plan(
        fixture(original_p="0.03", original_d="0.02", requested_p="0.02", requested_d="0.01")
    )
    assert first.lines[0].payment_amount.minor_units == 2
    final = plan(
        fixture(
            original_p="0.03",
            original_d="0.02",
            returned_p="0.02",
            returned_d="0.01",
            requested_p="0.01",
            requested_d="0.01",
        )
    )
    assert final.lines[0].payment_amount.minor_units == 1


@pytest.mark.parametrize("direction", ["receipt", "payout"])
def test_same_asset_partial_and_full_return_are_exactly_balanced(direction):
    result = plan(
        fixture(
            direction=direction,
            asset="USD",
            original_p="100",
            original_d="100",
            requested_p="20",
            requested_d="20",
        )
    )
    line = result.lines[0]
    assert not line.cross_asset
    assert (line.advance_amount + line.document_control_amount).minor_units == 0
    final = plan(
        fixture(
            direction=direction,
            asset="USD",
            original_p="100",
            original_d="100",
            returned_p="20",
            returned_d="20",
            requested_p="80",
            requested_d="80",
        )
    )
    assert final.payment_amount.to_string() == "80.00"


@pytest.mark.parametrize(
    "values,code",
    [
        (dict(requested_p="23"), "allocation_return_ratio"),
        (dict(requested_p="111", requested_d="100"), "allocation_return_exceeds_source"),
        (dict(requested_d="101"), "allocation_return_exceeds_source"),
        (
            dict(returned_p="22", returned_d="20", requested_p="88.01", requested_d="80"),
            "allocation_return_exceeds_source",
        ),
        (
            dict(original_p="0.01", original_d="1", requested_p="0.01", requested_d="0.01"),
            "allocation_return_too_small",
        ),
        (
            dict(original_p="0.01", original_d="0.02", requested_p="0.01", requested_d="0.01"),
            "allocation_return_residual",
        ),
        (dict(requested_p="0"), "amount_positive"),
        (dict(requested_d="-1"), "amount_positive"),
        (dict(requested_p="0.001"), "amount_precision"),
        (dict(requested_d="0.001"), "amount_precision"),
        (dict(requested_p="100000000000000000000"), "amount_range"),
    ],
)
def test_wrong_ratios_over_capacity_zero_precision_and_orphan_residuals_are_rejected(values, code):
    with pytest.raises(MoneyError) as error:
        plan(fixture(**values))
    assert error.value.code == code


@pytest.mark.parametrize(
    "field,value",
    [
        ("returned_payment", Amount.parse("111", ASSETS["USD"])),
        ("returned_document", Amount.parse("-1", ASSETS["EUR"])),
        ("returned_document", Amount.parse("100", ASSETS["EUR"])),
        ("returned_payment", Amount.parse("0", ASSETS["EUR"])),
        ("payment_amount", None),
        ("ledger_id", uuid4()),
        ("payment_id", uuid4()),
        ("party_id", uuid4()),
        ("document_kind", "invoice"),
        ("allocation_date", date(2026, 9, 14)),
    ],
)
def test_inconsistent_or_cross_ledger_original_sources_are_rejected(field, value):
    ledger, payment, source, command = fixture()
    with pytest.raises(MoneyError) as error:
        plan((ledger, payment, replace(source, **{field: value}), command))
    assert error.value.code == "allocation_return_integrity"


def test_return_date_cannot_precede_original_application_and_missing_source_is_rejected():
    ledger, payment, source, command = fixture()
    raw = command.model_dump()
    raw["return_date"] = "2026-09-29"
    with pytest.raises(MoneyError) as error:
        plan((ledger, payment, source, BusinessAllocationReturnCreate.model_validate(raw)))
    assert error.value.code == "allocation_return_date"
    with pytest.raises(MoneyError) as error:
        plan_allocation_return(ledger, payment, command, {}, ASSETS)
    assert error.value.code == "allocation_return_integrity"


@pytest.mark.parametrize("scale", [0, 2, 6, 18])
def test_ratio_handles_complete_token_identity_precision_and_maximum_exact_quantities(scale):
    token = stablecoin_asset(
        code="USDC", network="fictional", token_reference="test-token", scale=scale
    )
    catalog = {**ASSETS, token.asset_id: token}
    maximum = "99999999999999999999.99"
    document = "99999999999999999999" + ("." + "9" * scale if scale else "")
    with localcontext() as context:
        context.prec = 1
        result = plan(
            fixture(
                asset=token.asset_id,
                original_p=maximum,
                original_d=document,
                requested_p=maximum,
                requested_d=document,
                catalog=catalog,
            ),
            catalog,
        )
    assert result.lines[0].document_amount.to_string() == document
    assert result.payment_amount.to_string() == maximum


def test_disabled_or_changed_catalog_definitions_do_not_enable_new_returns():
    args = fixture()
    with pytest.raises(MoneyError) as error:
        plan(args, {**ASSETS, "USD": replace(ASSETS["USD"], enabled=False)})
    assert error.value.code == "asset_disabled"
    with pytest.raises(MoneyError) as error:
        plan(args, {**ASSETS, "EUR": fiat_asset(code="JPY", scale=0)})
    assert error.value.code == "allocation_return_integrity"


def test_partial_ratio_multiplies_beyond_database_precision_without_context_rounding():
    token = ASSETS["ETH"]
    with localcontext() as context:
        context.prec = 1
        result = plan(
            fixture(
                asset="ETH",
                original_p="99999999999999999999.99",
                original_d="2",
                requested_p="50000000000000000000.00",
                requested_d="1",
            )
        )
    assert result.payment_amount.minor_units == 5000000000000000000000
    assert result.lines[0].document_amount == Amount.parse("1", token)


def test_same_token_code_on_two_networks_still_uses_separate_asset_legs():
    paid = stablecoin_asset(code="USDC", network="fictional_a", token_reference="test", scale=2)
    billed = stablecoin_asset(code="USDC", network="fictional_b", token_reference="test", scale=2)
    catalog = {**ASSETS, paid.asset_id: paid, billed.asset_id: billed}
    ledger, payment, source, command = fixture(asset=billed.asset_id, catalog=catalog)
    payment = replace(payment, amount=Amount.parse("110", paid), applied=Amount.parse("110", paid))
    source = replace(source, payment_amount=payment.amount, returned_payment=Amount(paid, 0))
    result = plan((ledger, payment, source, command), catalog)
    assert result.lines[0].cross_asset
    assert result.lines[0].payment_amount.asset == paid
    assert result.lines[0].document_amount.asset == billed


def test_multiple_original_mappings_of_one_document_are_distinct_and_checked_as_a_group():
    ledger, payment, source, command = fixture()
    source = replace(
        source,
        payment_amount=Amount.parse("55", ASSETS["USD"]),
        document_amount=Amount.parse("50", ASSETS["EUR"]),
    )
    second = replace(source, id=uuid4(), operation_id=uuid4())
    raw = command.model_dump()
    raw["lines"].append(dict(raw["lines"][0], source_allocation_line_id=second.id))
    payload = BusinessAllocationReturnCreate.model_validate(raw)
    result = plan_allocation_return(
        ledger, payment, payload, {source.id: source, second.id: second}, ASSETS
    )
    assert result.payment_amount.to_string() == "44.00"
    assert [line.line_no for line in result.lines] == [1, 2]
    assert result.lines[0].source.document_line_id == result.lines[1].source.document_line_id
    with pytest.raises(MoneyError) as error:
        plan_allocation_return(
            ledger,
            replace(payment, applied=Amount.parse("43.99", ASSETS["USD"])),
            payload,
            {source.id: source, second.id: second},
            ASSETS,
        )
    assert error.value.code == "allocation_return_integrity"


@pytest.mark.parametrize(
    "field,value",
    [
        ("return_date", datetime(2026, 10, 1)),
        ("return_date", 1790812800),
        ("return_date", "2026-02-30"),
        ("reason", "  "),
        ("reason", "bad\x00reason"),
        ("reason", "x" * 2001),
        ("extra", True),
    ],
)
def test_original_command_requires_bounded_explicit_fields(field, value):
    raw = fixture()[3].model_dump()
    raw[field] = value
    with pytest.raises(ValidationError):
        BusinessAllocationReturnCreate.model_validate(raw)


@pytest.mark.parametrize(
    "field,value",
    [
        ("expected_version", True),
        ("expected_version", "1"),
        ("expected_version", 0),
        ("payment_amount", 22),
        ("document_amount", 20.0),
        ("document_amount", "2e1"),
    ],
)
def test_original_line_keeps_strict_versions_and_decimal_strings(field, value):
    raw = fixture()[3].model_dump()
    raw["lines"][0][field] = value
    with pytest.raises(ValidationError):
        BusinessAllocationReturnCreate.model_validate(raw)


def test_group_rejects_duplicate_originals_and_bounds_200_members():
    raw = fixture()[3].model_dump()
    raw["lines"] *= 2
    with pytest.raises(ValidationError):
        BusinessAllocationReturnCreate.model_validate(raw)
    raw["lines"] = [dict(raw["lines"][0], source_allocation_line_id=uuid4()) for _ in range(200)]
    assert len(BusinessAllocationReturnCreate.model_validate(raw).lines) == 200
    raw["lines"].append(dict(raw["lines"][0], source_allocation_line_id=uuid4()))
    with pytest.raises(ValidationError):
        BusinessAllocationReturnCreate.model_validate(raw)
    raw["lines"] = []
    with pytest.raises(ValidationError):
        BusinessAllocationReturnCreate.model_validate(raw)


def test_maximum_group_plans_all_200_original_mappings_without_dropping_the_last_one():
    ledger, payment, source, command = fixture(
        asset="USD", original_p="2", original_d="2", requested_p="0.01", requested_d="0.01"
    )
    unit = Amount.parse("0.01", ASSETS["USD"])
    originals = [
        replace(source, id=uuid4(), operation_id=uuid4(), payment_amount=unit, document_amount=unit)
        for _ in range(200)
    ]
    raw = command.model_dump()
    raw["lines"] = [dict(raw["lines"][0], source_allocation_line_id=item.id) for item in originals]
    result = plan_allocation_return(
        ledger,
        payment,
        BusinessAllocationReturnCreate.model_validate(raw),
        {item.id: item for item in originals},
        ASSETS,
    )
    assert len(result.lines) == 200 and result.lines[-1].line_no == 200
    assert result.payment_amount.to_string() == "2.00"
    assert sum(line.document_amount.minor_units for line in result.lines) == 200


def receipt_fixture():
    ledger, payment, source, command = fixture()
    posting = dict(
        line_no=1,
        source_allocation_line_id=source.id,
        source_operation_id=source.operation_id,
        document_id=source.document_id,
        document_line_id=source.document_line_id,
        payment_amount="22.00",
        document_amount="20.00",
        operation_id=uuid4(),
        journal_id=uuid4(),
    )
    return dict(
        kind="business_allocation_return",
        id=command.id,
        ledger_id=ledger,
        payment_id=payment.id,
        return_date=command.return_date,
        postings=[posting],
    )


def test_receipt_preserves_original_mapping_and_allows_separate_mappings_of_same_document_line():
    raw = receipt_fixture()
    raw["postings"].append(
        dict(
            raw["postings"][0],
            line_no=2,
            source_allocation_line_id=uuid4(),
            source_operation_id=uuid4(),
            operation_id=uuid4(),
            journal_id=uuid4(),
        )
    )
    receipt = BusinessAllocationReturnResponse.model_validate(raw)
    assert receipt.postings[0].document_line_id == receipt.postings[1].document_line_id
    assert (
        receipt.model_dump_json()
        == BusinessAllocationReturnResponse.model_validate_json(
            receipt.model_dump_json()
        ).model_dump_json()
    )
    with pytest.raises(ValidationError):
        receipt.kind = "business_allocation"


@pytest.mark.parametrize(
    "field",
    ["line_no", "source_allocation_line_id", "source_operation_id", "operation_id", "journal_id"],
)
def test_receipt_rejects_reordered_or_repeated_original_and_posting_members(field):
    raw = receipt_fixture()
    second = dict(
        raw["postings"][0],
        line_no=2,
        source_allocation_line_id=uuid4(),
        source_operation_id=uuid4(),
        operation_id=uuid4(),
        journal_id=uuid4(),
    )
    second[field] = raw["postings"][0][field]
    raw["postings"].append(second)
    with pytest.raises(ValidationError):
        BusinessAllocationReturnResponse.model_validate(raw)


def test_receipt_cannot_reuse_an_original_operation_as_its_new_return_operation():
    raw = receipt_fixture()
    raw["postings"][0]["operation_id"] = raw["postings"][0]["source_operation_id"]
    with pytest.raises(ValidationError):
        BusinessAllocationReturnResponse.model_validate(raw)


@pytest.mark.parametrize("field", ["payment_amount", "document_amount"])
@pytest.mark.parametrize("value", ["0", "0.00", "-1", "100000000000000000000", "0." + "1" * 19])
def test_success_receipt_rejects_nonpositive_or_out_of_storage_bound_quantities(field, value):
    raw = receipt_fixture()
    raw["postings"][0][field] = value
    with pytest.raises(ValidationError):
        BusinessAllocationReturnResponse.model_validate(raw)
