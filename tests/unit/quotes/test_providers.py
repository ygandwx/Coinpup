"""Exercise provider JSON and exact timestamps without market network traffic."""

from datetime import UTC, date, datetime
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import pytest
from coinpup_api.config import Settings
from coinpup_api.jobs import __main__ as entry
from coinpup_api.ledger import get_asset
from coinpup_api.ledger.assets import stablecoin_asset
from coinpup_api.quotes.providers import (
    COINBASE,
    ECB,
    fetch_market_observation,
    open_market_response,
)

DAY = date(2026, 11, 16)
NOW = datetime(2026, 11, 18, 20, tzinfo=UTC)
EUR, USD, BTC = (get_asset(asset) for asset in ("EUR", "USD", "BTC"))


def _fetch(payload, base=EUR, display=USD, price_date=None):
    return fetch_market_observation(
        base, display, price_date=price_date, fetch=lambda _: payload, clock=lambda: NOW
    )


def test_reference_data_is_exact_and_pinned_to_one_provider():
    requested = []

    def receive(url):
        requested.append(urlsplit(url))
        return b'[{"date":"2026-11-16","base":"EUR","quote":"USD","rate":1.123456789123456789}]'

    quote = fetch_market_observation(EUR, USD, price_date=DAY, fetch=receive, clock=lambda: NOW)
    (request,) = requested
    assert (request.scheme, request.hostname, request.path) == (
        "https",
        "api.frankfurter.dev",
        "/v2/rates",
    )
    assert parse_qs(request.query) == {
        "base": ["EUR"],
        "quotes": ["USD"],
        "providers": ["ecb"],
        "date": [DAY.isoformat()],
    }
    assert quote.price == "1.123456789123456789"
    assert (quote.source, quote.price_date, quote.observed_at, quote.fetched_at) == (
        ECB,
        DAY,
        None,
        NOW,
    )
    assert _fetch(b"[]", price_date=DAY) is None
    assert (
        _fetch(b'[{"date":"2026-11-13","base":"EUR","quote":"USD","rate":1.1}]', price_date=DAY)
        is None
    )


@pytest.mark.parametrize(
    "raw",
    [
        b'[{"date":"2026-11-16","base":"USD","quote":"EUR","rate":1.10}]',
        b'[{"date":"2026-11-16","base":"EUR","quote":"USD","rate":"1.10"}]',
        b'[{"date":"2026-11-16","base":"EUR","quote":"USD","rate":0}]',
        b'[{"date":"2026-11-16","base":"EUR","quote":"USD","rate":1e-100000}]',
        b'[{"date":"2026-11-16","base":"EUR","quote":"USD","rate":1e100000}]',
        b'[{"date":"2026-11-16","base":"EUR","quote":"USD","rate":NaN}]',
        b'[{"date":16,"base":"EUR","quote":"USD","rate":1.10}]',
        b'[{"date":"2026-11-16","base":"EUR","quote":"USD","rate":1.10,"rate":1.00}]',
    ],
)
def test_reference_response_rejects_inexact_or_untrusted_data(raw):
    with pytest.raises(ValueError):
        _fetch(raw)


def test_crypto_ticker_keeps_actual_time_and_only_uses_complete_allowed_assets():
    def receive(url):
        assert url == "https://api.exchange.coinbase.com/products/BTC-USD/ticker"
        return b'{"price":"67654.12345678","time":"2026-11-16T01:10:11.123456+01:00"}'

    quote = fetch_market_observation(BTC, USD, fetch=receive, clock=lambda: NOW)
    assert quote.price == "67654.12345678"
    assert quote.observed_at == datetime(2026, 11, 16, 0, 10, 11, 123456, tzinfo=UTC)
    assert (quote.source, quote.price_date, quote.fetched_at) == (COINBASE, DAY, NOW)
    token = stablecoin_asset(code="USDT", network="fixture", token_reference="fictional", scale=6)
    assert fetch_market_observation(token, USD, fetch=receive, clock=lambda: NOW) is None
    assert fetch_market_observation(BTC, get_asset("HKD"), fetch=receive, clock=lambda: NOW) is None
    with pytest.raises(ValueError):
        _fetch(b'{"price":"67654.12","time":"2026-11-16T00:10:11"}', base=BTC)
    with pytest.raises(ValueError):
        _fetch(b'{"price":"67654.12","time":"2026-11-19T00:10:11Z"}', base=BTC)
    with pytest.raises(ValueError):
        open_market_response("https://example.invalid/data")
    with pytest.raises(ValueError):
        open_market_response("http://api.frankfurter.dev/v2/rates")


def test_job_does_not_open_database_or_connect_without_explicit_opt_in(monkeypatch, capsys):
    monkeypatch.setattr(entry, "Settings", lambda: Settings(_env_file=None, environment="test"))

    def unavailable(*args):
        raise AssertionError("Opt-out must not instantiate a database or provider.")

    monkeypatch.setattr(entry, "Database", unavailable)
    assert (
        entry.main(["market-quotes", "--owner", str(uuid4()), "--asset", "EUR", "--display", "USD"])
        == 1
    )
    assert "disabled" in capsys.readouterr().err
