"""Traceable original groups and exact display totals using fictional reviewed line quotes."""

from dataclasses import replace
from datetime import UTC, datetime
from fractions import Fraction

import pytest
from coinpup_api.ledger import Amount, get_asset, stablecoin_asset
from coinpup_api.quotes.valuation import DirectQuote, Valuation
from coinpup_api.reporting.asset_totals import original_subtotals, valued_subtotals

EUR, USD = get_asset("EUR"), get_asset("USD")
T = datetime(2020, 1, 15, tzinfo=UTC)
H = DirectQuote(EUR, USD, "1.10", "Fictional market close", T, T)


def test_mixed_originals_have_no_unitless_sum_and_rows_keep_their_own_prices():
    original = [Amount(EUR, 10000), Amount(USD, 10000)]
    assert [(x.asset, x.quantity) for x in original_subtotals(reversed(original))] == [
        (EUR, "100.00"),
        (USD, "100.00"),
    ]
    rows = [
        Valuation(original[0], USD, H),
        Valuation(original[1], USD),
        Valuation(-original[0], USD, replace(H, price="1.20")),
    ]
    result = valued_subtotals(USD, iter(rows))
    assert [(x.asset, x.quantity) for x in result.originals] == [(EUR, "0.00"), (USD, "100.00")]
    assert result.missing_rows == 0 and result.display_quantity == "90.00"
    assert result.display.exact_known_minor == 9000
    assert rows[0].original == original[0] and rows[1].original == original[1]


def test_any_missing_row_within_same_asset_marks_only_a_partial_total():
    a = Amount(EUR, 1)
    rows = [
        Valuation(a, USD, replace(H, price="0.5")),
        Valuation(a, USD, replace(H, price="0.5")),
        Valuation(-a, USD),
    ]
    total = valued_subtotals(USD, rows)
    assert total.display_quantity is None and total.display.missing_assets == {EUR}
    assert total.display.exact_known_minor == 1 and total.missing_rows == 1
    assert total.originals[0].quantity == "0.01"
    assert valued_subtotals(USD, rows[:2]).display_quantity == "0.01"


def test_aggregates_beyond_posting_size_and_disabled_historical_assets_remain_exact():
    amount = Amount.parse("99999999999999999999.99", EUR)
    twice = original_subtotals([amount, Amount(replace(EUR, enabled=False), amount.minor_units)])
    assert len(twice) == 1 and twice[0].quantity == "199999999999999999999.98"
    result = valued_subtotals(USD, [Valuation(-amount, USD, replace(H, price="1"))] * 2)
    assert result.display_quantity == "-199999999999999999999.98"


def test_distinct_token_networks_and_scales_keep_separate_rows_and_missing_coverage():
    one = stablecoin_asset(code="USDT", network="testnet", token_reference="ABC", scale=6)
    two = replace(one, network="othernet")
    three = replace(one, scale=8)
    groups = original_subtotals([Amount(one, 2), Amount(two, 3), Amount(three, 4)])
    assert len(groups) == 3 and {g.asset: g.quantity for g in groups} == {
        one: "0.000002",
        two: "0.000003",
        three: "0.00000004",
    }
    report = valued_subtotals(
        USD,
        [
            Valuation(Amount(one, 2), USD, replace(H, base=one, price="0.99")),
            Valuation(Amount(two, 3), USD),
        ],
    )
    assert report.display.exact_known_minor == Fraction(198, 1000000)
    assert report.display.missing_assets == {two} and report.display_quantity is None


def test_empty_scope_and_mixed_targets_are_distinguished():
    assert original_subtotals([]) == ()
    empty = valued_subtotals(USD, [])
    assert empty.originals == () and empty.display_quantity == "0.00" and empty.missing_rows == 0
    with pytest.raises(ValueError):
        valued_subtotals(USD, [Valuation(Amount(EUR, 1), EUR)])
