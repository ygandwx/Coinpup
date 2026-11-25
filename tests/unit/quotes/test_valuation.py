"""Fictional prices and exact valuations independent of decimal context and ledger I/O."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone
from decimal import localcontext
from fractions import Fraction

import pytest
from coinpup_api.ledger import Amount, fiat_asset, get_asset, stablecoin_asset
from coinpup_api.quotes.valuation import DirectQuote, Valuation, total_valuations

EUR, USD = get_asset("EUR"), get_asset("USD")
DAY = datetime(2020, 1, 15, 18, tzinfo=timezone(timedelta(hours=8)))
H = DirectQuote(EUR, USD, "1.10", "Fictional historical", DAY, DAY + timedelta(hours=3))


def value(text, quote=H):
    return Valuation(Amount.parse(text, quote.base), quote.display, quote)


def test_historical_and_latest_preserve_exact_source_amount_and_witness():
    latest = replace(
        H,
        price="1.20",
        source="Fictional latest",
        effective_at=DAY + timedelta(days=1),
        fetched_at=DAY + timedelta(days=2),
    )
    historic, current = value("100"), value("100", latest)
    assert historic.original == current.original == Amount(EUR, 10000)
    assert historic.exact_minor == 11000 and current.exact_minor == 12000
    assert historic.quote.effective_at == datetime(2020, 1, 15, 10, tzinfo=UTC)
    assert current.quote.fetched_at > current.quote.effective_at


@pytest.mark.parametrize("code", ["GBP", "USD", "EUR", "HKD", "CNY"])
def test_saved_display_currency_needs_no_invented_price(code):
    asset = get_asset(code)
    row = Valuation(Amount(asset, 37), asset)
    assert row.quote is None and row.exact_minor == 37
    assert total_valuations(asset, [row]).rounded_minor == 37


def test_missing_quote_cannot_turn_a_known_subtotal_into_a_complete_report():
    known = value("0.01")
    missing = Valuation(Amount(get_asset("BTC"), 0), USD)
    total = total_valuations(USD, [known, missing])
    assert known.exact_minor == total.exact_known_minor == Fraction(11, 10)
    assert missing.exact_minor is None and total.rounded_minor is None
    assert total.missing_assets == {get_asset("BTC")}
    with pytest.raises(ValueError):
        total_valuations(EUR, [known])


def test_stablecoin_not_par_and_wrong_network_or_token_does_not_match():
    token = stablecoin_asset(
        code="USDT", network="testnet", token_reference="ExampleAlias", scale=6
    )
    price = replace(H, base=token, price="0.99")
    row = value("100", price)
    assert row.exact_minor == 9900
    assert Valuation(Amount(replace(token, enabled=False), 100_000_000), USD, price) == row
    for other in [
        replace(token, network="othernet"),
        replace(token, token_reference="exampleAlias"),
        replace(token, scale=8),
        EUR,
    ]:
        with pytest.raises(ValueError):
            Valuation(Amount(other, 1), USD, price)


@pytest.mark.parametrize("sign", [1, -1])
def test_rounds_after_sum_and_half_ties_stay_symmetric(sign):
    tiny = Valuation(Amount(EUR, sign), USD, replace(H, price="0.5"))
    assert tiny.exact_minor == Fraction(sign, 2)
    assert total_valuations(USD, [tiny]).rounded_minor == sign
    assert total_valuations(USD, [tiny, tiny]).rounded_minor == sign
    assert (
        total_valuations(USD, [tiny, Valuation(-tiny.original, USD, tiny.quote)]).rounded_minor == 0
    )


def test_large_and_subminor_values_remain_exact_independent_of_decimal_context():
    src = get_asset("ETH")
    amount = Amount.parse("99999999999999999999.999999999999999999", src)
    h = replace(H, base=src, price="99999999999999999999.999999999999999999")
    with localcontext() as context:
        context.prec = 2
        expected = Fraction((10**38 - 1) ** 2, 10**34)
        assert Valuation(amount, USD, h).exact_minor == expected
        sub = Valuation(Amount(src, 1), USD, replace(h, price="0.000000000000000001"))
        assert sub.exact_minor == Fraction(1, 10**34)
        assert total_valuations(USD, [sub]).rounded_minor == 0


@pytest.mark.parametrize(
    "price",
    [
        0,
        0.1,
        True,
        None,
        "0",
        "0.000",
        "-1",
        "1e-2",
        "01",
        " 1",
        "1\n",
        "０.１",
        "9" * 21,
        "0." + "1" * 19,
    ],
)
def test_rejects_inexact_or_unbounded_rate(price):
    with pytest.raises(ValueError):
        replace(H, price=price)


@pytest.mark.parametrize(
    "change",
    [
        {"source": " "},
        {"source": "fake\nheader"},
        {"effective_at": DAY.replace(tzinfo=None)},
        {"fetched_at": None},
        {"base": USD},
        {"display": fiat_asset(code="JPY", scale=0)},
        {"display": get_asset("BTC")},
    ],
)
def test_ambiguous_price_evidence_is_refused(change):
    with pytest.raises(ValueError):
        replace(H, **change)
