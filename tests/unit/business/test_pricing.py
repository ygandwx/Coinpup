"""Fictional prices: independent rational oracle, rounding, bounds and hostile inputs."""

from dataclasses import FrozenInstanceError, replace
from decimal import Decimal, localcontext
from fractions import Fraction
from random import Random

import pytest
from coinpup_api.business.pricing import PriceInput, price_document, price_line
from coinpup_api.ledger.assets import fiat_asset, get_asset, stablecoin_asset
from coinpup_api.ledger.errors import MoneyError

USD = get_asset("USD")


@pytest.mark.parametrize(
    "value,expected",
    [
        (PriceInput("3", "19.99", "9.97", "8.25"), ("50.00", "4.13", "54.13")),
        (PriceInput("1.005", "1", "0.01"), ("1.00", "0.00", "1.00")),
        (PriceInput("0.5", "0.01", tax_rate_percent="50"), ("0.01", "0.01", "0.02")),
        (PriceInput("0.49", "0.01", tax_rate_percent="100"), ("0.00", "0.00", "0.00")),
        (PriceInput("2", "5", "10", "20"), ("0.00", "0.00", "0.00")),
        (PriceInput("1", "0", "0", "20"), ("0.00", "0.00", "0.00")),
        (PriceInput("1", "1", tax_rate_percent="150"), ("1.00", "1.50", "2.50")),
        (
            PriceInput("1", "10", tax_rate_percent="0.000000000000000001"),
            ("10.00", "0.00", "10.00"),
        ),
    ],
)
def test_fictional_pricing_examples(value, expected):
    result = price_line(value, USD)
    assert tuple(item.to_string() for item in (result.net, result.tax, result.total)) == expected
    assert result.total == result.net + result.tax


def test_total_is_sum_of_rounded_lines_not_recomputed_tax_or_aggregate_rounding():
    line = PriceInput("0.5", "0.01", tax_rate_percent="50")
    result = price_document([line, line], USD)
    assert result.net.to_string() == "0.02"
    assert result.tax.to_string() == "0.02"
    assert result.total.to_string() == "0.04"
    assert price_line(replace(line, quantity="1"), USD).total.to_string() == "0.02"


@pytest.mark.parametrize(
    "asset",
    [
        fiat_asset(code="JPY", scale=0),
        USD,
        get_asset("BTC"),
        get_asset("XMR"),
        get_asset("ETH"),
        stablecoin_asset(code="USDT", network="fictional", token_reference="fictional", scale=6),
    ],
)
def test_minimum_unit_rounding_and_independent_fraction_oracle(asset):
    minimum = "0." + "0" * (asset.scale - 1) + "1" if asset.scale else "1"
    result = price_line(PriceInput("0.5", minimum, tax_rate_percent="50"), asset)
    assert (result.net.minor_units, result.tax.minor_units, result.total.minor_units) == (1, 1, 2)
    rng = Random(2609)
    for _ in range(80):
        quantity = f"{rng.randrange(1, 100)}.{rng.randrange(100):02}"
        unit_price = str(rng.randrange(1, 100))
        rate = f"{rng.randrange(300)}.{rng.randrange(1000):03}"
        # Fixed one-unit discount is at most the exact positive gross in these cases.
        value = PriceInput(quantity, unit_price, "1", rate)
        result = price_line(value, asset)
        raw_net = (Fraction(quantity) * Fraction(unit_price) - 1) * 10**asset.scale
        net = (raw_net + Fraction(1, 2)).__floor__()
        tax = (net * Fraction(rate) / 100 + Fraction(1, 2)).__floor__()
        assert (result.net.minor_units, result.tax.minor_units) == (net, tax)
        assert result.total.minor_units == net + tax
        assert result.total.asset == asset


@pytest.mark.parametrize("field", ["quantity", "tax_rate_percent"])
@pytest.mark.parametrize(
    "bad",
    [
        None,
        True,
        1,
        0.5,
        Decimal("1"),
        "",
        " 1",
        "1\n",
        "01",
        "+1",
        "-0",
        "-1",
        ".1",
        "1.",
        "1e2",
        "NaN",
        "Infinity",
        "１",
        "١",
        "1,000",
        "1_0",
        "1\x00",
        "1" * 10000,
        "100000000000000000000",
        "0.0000000000000000001",
    ],
)
def test_ratio_fields_reject_ambiguous_or_unbounded_inputs(field, bad):
    with pytest.raises(MoneyError) as error:
        price_line(replace(PriceInput("1", "1"), **{field: bad}), USD)
    assert error.value.code == ("pricing_quantity" if field == "quantity" else "pricing_tax_rate")


@pytest.mark.parametrize("quantity", ["0", "0.0", "0.000000000000000000"])
def test_zero_quantity_rejected(quantity):
    with pytest.raises(MoneyError, match="positive"):
        price_line(PriceInput(quantity, "1"), USD)


@pytest.mark.parametrize("field", ["unit_price", "discount_amount"])
@pytest.mark.parametrize("bad", [True, 1, Decimal("1"), "1e1", "0.001", "1.000", " 1"])
def test_price_and_discount_use_existing_exact_asset_amount_contract(field, bad):
    with pytest.raises(MoneyError):
        price_line(replace(PriceInput("1", "10"), **{field: bad}), USD)


@pytest.mark.parametrize(
    "value,code",
    [
        (PriceInput("1", "-1"), "pricing_price"),
        (PriceInput("1", "1", "-0.01"), "pricing_discount"),
        (PriceInput("1", "1", "1.01"), "pricing_discount"),
        (PriceInput("0.5", "0.01", "0.01"), "pricing_discount"),
    ],
)
def test_discount_cannot_exceed_exact_gross_even_when_rounded_gross_would_allow_it(value, code):
    with pytest.raises(MoneyError) as error:
        price_line(value, USD)
    assert error.value.code == code


def test_storage_bounds_and_adversarial_decimal_context():
    maximum = "99999999999999999999.99"
    with localcontext() as ctx:
        ctx.prec = 1
        for signal in ctx.traps:
            ctx.traps[signal] = True
        assert price_line(PriceInput("1", maximum), USD).total.to_string() == maximum
        assert (
            price_line(PriceInput("1.005", "1", tax_rate_percent="50"), USD).total.to_string()
            == "1.52"
        )
        values = [
            [PriceInput("2", maximum)],
            [PriceInput("1", maximum, tax_rate_percent="1")],
            [PriceInput("1", "2", tax_rate_percent="99999999999999999999")],
            [PriceInput("1", maximum), PriceInput("1", "0.01")],
        ]
        # The third case has a representable result; large rates are not silently capped.
        assert price_document(values.pop(2), USD).tax.to_string() == "1999999999999999999.98"
        for rows in values:
            with pytest.raises(MoneyError) as error:
                price_document(rows, USD)
            assert error.value.code == "amount_range"
        with pytest.raises(MoneyError) as error:
            price_line(PriceInput("1", maximum, tax_rate_percent="99999999999999999999"), USD)
        assert error.value.code == "amount_range"


def test_draft_bounds_immutability_and_input_preservation():
    empty = price_document([], USD)
    assert empty.lines == () and empty.total.to_string() == "0.00"
    value = PriceInput("1.00", "1.0", "0.00", "0.000")
    rows = [value] * 200
    result = price_document(rows, USD)
    rows.clear()
    assert result.total.to_string() == "200.00" and len(result.lines) == 200
    assert value == PriceInput("1.00", "1.0", "0.00", "0.000")
    with pytest.raises(FrozenInstanceError):
        result.total = empty.total
    for bad in ([value] * 201, None, "1", (x for x in [])):
        with pytest.raises(MoneyError) as error:
            price_document(bad, USD)
        assert error.value.code == "pricing_lines"
    with pytest.raises(MoneyError) as error:
        price_document([{}], USD)
    assert error.value.code == "pricing_line"


@pytest.mark.parametrize("rows", [[], [PriceInput("1", "1")]])
def test_empty_and_nonempty_drafts_require_complete_enabled_asset(rows):
    for asset in ("USD", None, replace(USD, enabled=False)):
        with pytest.raises(MoneyError):
            price_document(rows, asset)
