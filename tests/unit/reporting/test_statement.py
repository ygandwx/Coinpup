from dataclasses import replace
from datetime import UTC, date, datetime, time, timedelta
from uuid import UUID

import pytest
from coinpup_api.ledger import Amount, get_asset, stablecoin_asset
from coinpup_api.quotes.valuation import DirectQuote
from coinpup_api.reporting.statement import (
    CashRepaymentLink,
    ManualPrice,
    SourceRow,
    build_statement,
)

DAY = date(2026, 11, 25)
PAYER, BENEFICIARY, OTHER = (UUID(int=n) for n in range(1, 4))
EUR, USD = get_asset("EUR"), get_asset("USD")


def row(ledger, source, amount, asset=USD):
    return SourceRow(ledger, UUID(int=source), Amount.parse(amount, asset))


def quote(base=EUR, rate="1.10", day=DAY, target=USD):
    stamp = datetime.combine(day, time.min, UTC)
    return ManualPrice(day, DirectQuote(base, target, rate, "Fictional manual", stamp, stamp))


def amounts(statement):
    return {entry.asset: entry.quantity for entry in statement.original_totals}


def test_selected_date_preserves_original_groups_and_explicit_price_evidence():
    token = stablecoin_asset(code="USDT", network="fictional-m5", token_reference="A", scale=6)
    other_token = replace(token, token_reference="B")
    sources = [
        row(PAYER, 10, "100.00", EUR),
        row(PAYER, 10, "50.00", USD),
        row(BENEFICIARY, 10, "100.000000", token),
        row(BENEFICIARY, 11, "7.000000", other_token),
    ]
    wrong_day = quote(day=DAY - timedelta(days=1), rate="1.20")
    exact_eur = quote()
    exact_token = quote(token, "0.99")
    statement = build_statement(
        [PAYER, BENEFICIARY],
        iter(sources),
        valuation_day=DAY,
        display=USD,
        manual_prices=(wrong_day, exact_eur, exact_token, quote(other_token, "1", wrong_day.day)),
    )
    assert tuple(entry.source for entry in statement.rows) == tuple(sources)
    assert amounts(statement) == {
        EUR: "100.00",
        USD: "50.00",
        token: "100.000000",
        other_token: "7.000000",
    }
    assert tuple(entry.price for entry in statement.rows) == (exact_eur, None, exact_token, None)
    valuation = statement.valuation
    assert valuation.display.exact_known_minor == 25900
    assert valuation.display.missing_assets == {other_token}
    assert valuation.missing_rows == 1
    assert valuation.display_quantity is None


def test_missing_exact_day_does_not_use_previous_day_or_reverse_rate():
    statement = build_statement(
        [PAYER],
        [row(PAYER, 12, "1.00", EUR)],
        valuation_day=DAY,
        display=USD,
        manual_prices=(quote(day=DAY - timedelta(days=1)), quote(USD, "1", target=EUR)),
    )
    assert amounts(statement) == {EUR: "1.00"}
    assert statement.valuation.display_quantity is None


def test_only_explicit_balanced_repayments_are_excluded_when_both_cash_legs_are_shown():
    external = row(PAYER, 20, "-100.00")
    payment = row(BENEFICIARY, 21, "-40.00")
    receipt = row(PAYER, 22, "40.00")
    unrelated = row(BENEFICIARY, 23, "40.00")
    link = CashRepaymentLink(UUID(int=50), payment.reference, receipt.reference)

    statement = build_statement(
        [PAYER, BENEFICIARY], [external, payment, receipt, unrelated], cash_repayments=[link]
    )
    assert amounts(statement) == {USD: "-60.00"}
    assert [item.excluded_repayment_group for item in statement.rows] == [
        None,
        link.group_id,
        link.group_id,
        None,
    ]
    assert [item.source.original.to_string() for item in statement.rows] == [
        "-100.00",
        "-40.00",
        "40.00",
        "40.00",
    ]

    only_payer = build_statement([PAYER], [external, receipt], cash_repayments=[link])
    assert amounts(only_payer) == {USD: "-60.00"}
    assert all(item.excluded_repayment_group is None for item in only_payer.rows)
    only_beneficiary = build_statement([BENEFICIARY], [payment], cash_repayments=[link])
    assert amounts(only_beneficiary) == {USD: "-40.00"}


@pytest.mark.parametrize(
    "receipt",
    [row(PAYER, 26, "39.99"), row(PAYER, 26, "40.00", EUR), row(BENEFICIARY, 26, "40.00")],
)
def test_invalid_stored_pairs_cannot_hide_cash_movements(receipt):
    payment = row(BENEFICIARY, 25, "-40.00")
    link = CashRepaymentLink(UUID(int=51), payment.reference, receipt.reference)
    with pytest.raises(ValueError, match="Inconsistent internal repayment"):
        build_statement([PAYER, BENEFICIARY], [payment, receipt], cash_repayments=[link])


def test_duplicate_or_outside_rows_fail_closed():
    owned = row(PAYER, 30, "100.00")
    for invalid in ([owned, owned], [owned, row(OTHER, 30, "100.00")]):
        with pytest.raises(ValueError, match="Unexpected or duplicate original row"):
            build_statement([PAYER], invalid)


def test_ambiguous_prices_and_incomplete_valuation_request_are_rejected():
    source = [row(PAYER, 33, "100.00", EUR)]
    with pytest.raises(ValueError, match="valuation date"):
        build_statement([PAYER], source, display=USD)
    with pytest.raises(ValueError, match="ambiguous"):
        build_statement(
            [PAYER],
            source,
            valuation_day=DAY,
            display=USD,
            manual_prices=[quote(), quote(rate="1.20")],
        )
