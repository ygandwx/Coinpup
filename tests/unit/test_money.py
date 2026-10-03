"""Quantity contract tests: precision, contexts, hostile input and typed SQL binding."""

from dataclasses import FrozenInstanceError, replace
from decimal import ROUND_UP, Decimal, localcontext

import pytest
from coinpup_api.ledger import Amount, AmountNumeric, MoneyError, get_asset, stablecoin_asset
from sqlalchemy.dialects import sqlite
from sqlalchemy.dialects.postgresql import psycopg

USD = get_asset("USD")
ETH = get_asset("ETH")
BTC = get_asset("BTC")


@pytest.mark.parametrize(
    "code,text,expected",
    [
        ("USD", "123.45", "123.45"),
        ("GBP", "-23.4", "-23.40"),
        ("EUR", "90", "90.00"),
        ("HKD", "0.01", "0.01"),
        ("CNY", "0", "0.00"),
        ("BTC", "0.12345678", "0.12345678"),
        ("ETH", "1.000000000000000001", "1.000000000000000001"),
        ("XMR", "0.123456789012", "0.123456789012"),
    ],
)
def test_exact_parse_and_serialization(code, text, expected):
    asset = get_asset(code)
    amount = Amount.parse(text, asset)
    assert amount.to_string() == expected
    assert amount.to_decimal() == Decimal(expected)
    assert Amount.from_decimal(amount.to_decimal(), asset) == amount
    with pytest.raises(FrozenInstanceError):
        amount.minor_units = 0


@pytest.mark.parametrize(
    "code,minimum,too_small",
    [
        ("USD", "0.01", "0.001"),
        ("BTC", "0.00000001", "0.000000001"),
        ("ETH", "0.000000000000000001", "0.0000000000000000001"),
        ("XMR", "0.000000000001", "0.0000000000001"),
    ],
)
def test_minimum_unit_and_excess_precision_are_distinct(code, minimum, too_small):
    asset = get_asset(code)
    assert Amount.parse(minimum, asset).minor_units == 1
    for text in (too_small, minimum + "0"):
        with pytest.raises(MoneyError) as error:
            Amount.parse(text, asset)
        assert error.value.code == "amount_precision"


@pytest.mark.parametrize("value", [1, 0.1, True, None, Decimal("0.1"), b"0.1", []])
def test_wire_input_must_be_a_string(value):
    with pytest.raises(MoneyError) as error:
        Amount.parse(value, USD)
    assert error.value.code == "amount_type"


@pytest.mark.parametrize(
    "text",
    [
        "",
        " ",
        " 1",
        "1 ",
        "1\n",
        "+1",
        "01",
        "-01",
        ".5",
        "1.",
        "1,000",
        "1_000",
        "1e2",
        "1E-2",
        "NaN",
        "sNaN",
        "Infinity",
        "-Infinity",
        "１２.３４",
        "١.٢",
        "1\x00",
        "--1",
    ],
)
def test_only_plain_ascii_decimal_syntax_is_accepted(text):
    with pytest.raises(MoneyError) as error:
        Amount.parse(text, USD)
    assert error.value.code == "amount_format"


def test_input_length_is_bounded_before_integer_conversion():
    with pytest.raises(MoneyError) as error:
        Amount.parse("9" * 100000, ETH)
    assert error.value.code == "amount_range"


@pytest.mark.parametrize("value", [True, 1.0, Decimal("1"), "1"])
def test_direct_construction_does_not_accept_coerced_minimum_units(value):
    with pytest.raises(MoneyError) as error:
        Amount(USD, value)
    assert error.value.code == "amount_units"


@pytest.mark.parametrize("asset", [USD, ETH, BTC, get_asset("XMR")])
def test_symmetric_storage_bounds_and_arithmetic_overflow(asset):
    maximum = Amount(asset, 10 ** (20 + asset.scale) - 1)
    assert Amount.parse(maximum.to_string(), asset) == maximum
    assert Amount.from_decimal(maximum.to_decimal(), asset) == maximum
    negative = -maximum
    assert -negative == maximum
    assert (maximum + -maximum).minor_units == 0
    for operation in (
        lambda: Amount(asset, 10 ** (20 + asset.scale)),
        lambda: Amount(asset, -(10 ** (20 + asset.scale))),
        lambda: maximum + Amount(asset, 1),
        lambda: -maximum - Amount(asset, 1),
        lambda: Amount.parse("100000000000000000000", asset),
    ):
        with pytest.raises(MoneyError) as error:
            operation()
        assert error.value.code == "amount_range"


def test_fees_and_transfer_legs_preserve_their_own_units():
    remaining = Amount.parse("1", BTC) - Amount.parse("0.1", BTC) - Amount.parse("0.00001", BTC)
    assert remaining.to_string() == "0.89999000"
    usd_out = Amount.parse("100", USD)
    eur_in = Amount.parse("90", get_asset("EUR"))
    usd_fee = Amount.parse("2", USD)
    assert (Amount.parse("1000", USD) - usd_out - usd_fee).to_string() == "898.00"
    assert eur_in.to_string() == "90.00"
    assert usd_out != eur_in
    for operation in (lambda: usd_out + eur_in, lambda: usd_out < eur_in):
        with pytest.raises(MoneyError) as error:
            operation()
        assert error.value.code == "asset_mismatch"


def test_numeric_comparison_does_not_follow_lexicographic_string_order():
    assert Amount.parse("2", USD) < Amount.parse("10", USD)
    assert Amount.parse("-10", USD) < Amount.parse("-2", USD)
    assert Amount.parse("2", USD) <= Amount.parse("2.00", USD)
    with pytest.raises(TypeError):
        Amount.parse("1", USD) + 1


def test_database_decimal_padding_and_negative_zero_are_lossless():
    assert Amount.from_decimal(Decimal("1.000000000000000000"), USD).to_string() == "1.00"
    assert Amount.from_decimal(Decimal("1.230000000000000000"), USD).to_string() == "1.23"
    assert Amount.parse("-0.00", USD).to_string() == "0.00"
    assert Amount.from_decimal(Decimal("-0E-999999999"), ETH).to_string() == "0." + "0" * 18
    with pytest.raises(MoneyError) as error:
        Amount.from_decimal(Decimal("1.230000000000000001"), USD)
    assert error.value.code == "amount_precision"


@pytest.mark.parametrize(
    "value,code",
    [
        (Decimal("1E+999999999"), "amount_range"),
        (Decimal("-1E+999999999"), "amount_range"),
        (Decimal("1E-999999999"), "amount_precision"),
        (Decimal("NaN"), "amount_finite"),
        (Decimal("sNaN"), "amount_finite"),
        (Decimal("Infinity"), "amount_finite"),
        (Decimal("-Infinity"), "amount_finite"),
        (0.1, "amount_type"),
        ("1.00", "amount_type"),
        (True, "amount_type"),
    ],
)
def test_database_decode_refuses_nonfinite_inexact_and_extreme_values(value, code):
    with pytest.raises(MoneyError) as error:
        Amount.from_decimal(value, ETH)
    assert error.value.code == code


def test_disabled_asset_rejects_new_input_but_preserves_historical_data():
    disabled = replace(USD, enabled=False)
    with pytest.raises(MoneyError) as error:
        Amount.parse("1", disabled)
    assert error.value.code == "asset_disabled"
    historical = Amount.from_decimal(Decimal("1.000000000000000000"), disabled)
    assert historical == Amount.parse("1", USD)
    assert historical.to_string() == "1.00"


def test_explicit_zero_scale_token_preserves_whole_units():
    asset = stablecoin_asset(
        code="USDT", network="test-chain", token_reference="WholeUnit", scale=0
    )
    assert Amount.parse("123", asset).to_string() == "123"
    assert Amount.parse("-0", asset).to_string() == "0"
    assert Amount.from_decimal(Decimal("123.000000000000000000"), asset).minor_units == 123
    with pytest.raises(MoneyError):
        Amount.parse("123.0", asset)
    with pytest.raises(MoneyError):
        Amount.from_decimal(Decimal("123.1"), asset)


def context_snapshot(context):
    return (
        context.prec,
        context.rounding,
        context.Emin,
        context.Emax,
        context.capitals,
        context.clamp,
        dict(context.traps),
        dict(context.flags),
    )


def test_all_operations_ignore_and_preserve_hostile_decimal_context():
    almost_max = "99999999999999999999.999999999999999998"
    expected = "99999999999999999999.999999999999999999"
    stored = Decimal(almost_max)
    with localcontext() as context:
        context.prec = 2
        context.rounding = ROUND_UP
        context.Emin, context.Emax, context.clamp = -2, 2, 1
        for signal in context.traps:
            context.traps[signal] = True
        before = context_snapshot(context)
        amount = Amount.from_decimal(stored, ETH)
        unit = Amount.parse("0.000000000000000001", ETH)
        maximum = amount + unit
        assert maximum.to_string() == expected
        assert Amount.from_decimal(maximum.to_decimal(), ETH) == maximum
        negative = -maximum
        assert -negative == maximum
        assert maximum - unit == amount
        assert amount < maximum
        assert AmountNumeric(ETH).process_bind_param(maximum, None).as_tuple() == (
            0,
            tuple(int(character) for character in expected.replace(".", "")),
            -18,
        )
        assert context_snapshot(context) == before


@pytest.mark.parametrize(
    "value",
    [
        0.1,
        True,
        1,
        "1.00",
        Decimal("1.00"),
        Decimal("1.001"),
        Decimal("0.0000000000000000001"),
        Decimal("NaN"),
    ],
)
def test_actual_sqlalchemy_bind_processor_rejects_unvalidated_values(value):
    dialect = psycopg.dialect()
    processor = AmountNumeric(USD).dialect_impl(dialect).bind_processor(dialect)
    with pytest.raises(MoneyError) as error:
        processor(value)
    assert error.value.code == "amount_required"


def test_sqlalchemy_binding_and_result_decoding_use_complete_asset_definition():
    asset = stablecoin_asset(code="USDT", network="test-chain", token_reference="TokenA", scale=6)
    column_type = AmountNumeric(asset)
    dialect = psycopg.dialect()
    bind = column_type.dialect_impl(dialect).bind_processor(dialect)
    assert bind(Amount.parse("1.234567", asset)) == Decimal("1.234567")
    for other in (replace(asset, scale=18), replace(asset, code="USDC"), USD):
        with pytest.raises(MoneyError) as error:
            bind(Amount.parse("1", other))
        assert error.value.code == "asset_mismatch"
    assert column_type.process_result_value(Decimal("1.234567000000000000"), None) == (
        Amount.parse("1.234567", asset)
    )
    assert bind(None) is None
    assert column_type.process_result_value(None, None) is None
    with pytest.raises(MoneyError) as error:
        AmountNumeric(USD).load_dialect_impl(sqlite.dialect())
    assert error.value.code == "unsupported_database"
