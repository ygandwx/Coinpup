"""Immutable local cache, safe report precedence, and the authenticated mobile quote page."""

import json
import runpy
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from coinpup_api.ledger.posting import PostingService
from coinpup_api.ledger.schemas import AccountCreate, EntityCreate
from coinpup_api.ledger.service import LedgerService
from coinpup_api.quotes.market import MarketQuoteService
from coinpup_api.sync.service import ChangeService
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from tests.integration.ledger.test_posting_service_database import (
    classified,
    financial_counts,
    opening,
)

pytestmark = pytest.mark.integration
QUOTES = "/api/v1/quotes"


def _fx(day, price):
    payload = f'[{{"date":"{day}","base":"EUR","quote":"USD","rate":{price}}}]'
    return lambda _: payload.encode()


def _report(client, ledger, mode=None, *, day=None, view="operating", **filters):
    valuation = {"display_asset_id": "USD"} if day or mode else {}
    if day is not None:
        valuation["price_date"] = day.isoformat()
    if mode is not None:
        valuation["price_mode"] = mode
    response = client.get(
        "/api/v1/reports/mvp", params={"ledger_ids": ledger, "view": view} | valuation | filters
    )
    assert response.status_code == 200, response.text
    return response.json()


def _manual(client, day, amount):
    response = client.put(
        QUOTES + "/manual",
        json={
            "asset_id": "EUR",
            "display_asset_id": "USD",
            "price_date": day.isoformat(),
            "price": amount,
            "source": "Fictional reviewed rate",
            "expected_version": 0,
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_exact_dates_manual_precedence_latest_conflict_and_failure_retain_originals(
    authenticated_client,
):
    client, engine, owner = authenticated_client
    structure = LedgerService(engine)
    ledger = structure.create_entity(
        owner,
        EntityCreate(
            kind="personal",
            name="Fictional quote ledger",
            base_asset_id="EUR",
            template_key="personal_default",
        ),
    ).ledger.id
    s = {
        "account": structure.create_account(
            owner,
            ledger,
            AccountCreate(name="Fictional bank", kind="bank", asset_ids=["USD", "EUR"]),
        ),
        "expenses": [
            item for item in structure.list_categories(owner, ledger) if item.kind == "expense"
        ],
        "posting": PostingService(engine),
    }
    now = datetime.now(UTC)
    older, newer = now.date() - timedelta(days=7), now.date() - timedelta(days=4)
    s["posting"].post_opening(
        owner,
        ledger,
        opening(s, "100.00", "EUR", transaction_date=older.isoformat()),
        "fictional-opening",
    )
    s["posting"].post_expense(
        owner,
        ledger,
        classified(
            s,
            amount="10.00",
            asset="EUR",
            transaction_date=older.isoformat(),
            recognition_date=newer.isoformat(),
        ),
        "fictional-expense",
    )
    baseline = financial_counts(engine)
    prices = MarketQuoteService(engine)
    first = prices.refresh(
        owner, "EUR", "USD", price_date=older, fetch=_fx(older, "1.10"), clock=lambda: now
    )
    assert (
        prices.refresh(
            owner,
            "EUR",
            "USD",
            price_date=older,
            fetch=_fx(older, "1.10"),
            clock=lambda: now + timedelta(minutes=2),
        )
        == first
    )
    second = prices.refresh(
        owner, "EUR", "USD", price_date=newer, fetch=_fx(newer, "1.30"), clock=lambda: now
    )
    assert first != second
    original = _report(client, ledger)
    assert original["valuation"] is None and original["rows"][0]["date"] == newer.isoformat()
    assert "automatic_price" not in original["rows"][0]
    exact = _report(client, ledger, day=older)
    witness = exact["rows"][0]["automatic_price"]
    assert exact["valuation"]["amount"] == "-11.00" and exact["rows"][0]["price"] is None
    assert witness["id"] == str(first) and witness["observed_at"] is None
    assert datetime.fromisoformat(witness["fetched_at"]) == now
    assert _report(client, ledger, "historical")["valuation"]["amount"] == "-11.00"
    assert _report(client, ledger, "latest")["valuation"]["amount"] == "-13.00"

    manual = _manual(client, older, "1.20")
    historical = _report(client, ledger, "historical")
    assert historical["valuation"]["amount"] == "-12.00"
    assert (
        historical["rows"][0]["price"] == manual
        and historical["rows"][0].get("automatic_price") is None
    )
    assert historical["originals"] == original["originals"]
    assert _report(client, ledger, "latest")["valuation"]["amount"] == "-13.00"

    def network_failure(_):
        raise OSError("Fictional provider outage")

    with pytest.raises(OSError):
        prices.refresh(owner, "EUR", "USD", fetch=network_failure, clock=lambda: now)
    assert _report(client, ledger, "latest")["rows"][0]["automatic_price"]["id"] == str(second)
    prices.refresh(
        owner, "EUR", "USD", price_date=newer, fetch=_fx(newer, "1.35"), clock=lambda: now
    )
    ambiguous = _report(client, ledger, "latest")
    assert ambiguous["valuation"]["amount"] is None
    assert ambiguous["valuation"]["missing_asset_ids"] == ["EUR"]
    assert ambiguous["rows"][0]["valuation_state"] == "missing"
    assert _report(client, ledger, day=older)["valuation"]["amount"] == "-12.00"
    _manual(client, newer, "1.40")
    assert _report(client, ledger, "latest")["valuation"]["amount"] == "-14.00"
    old_balance = _report(client, ledger, "historical", view="balances", to_date=older.isoformat())
    assert old_balance["valuation"]["amount"] == "108.00"
    assert _report(client, ledger, "latest", view="balances")["valuation"]["amount"] == "126.00"
    assert financial_counts(engine) == baseline

    changes = ChangeService(engine).list_changes(owner, limit=200).changes
    quotes = [row for row in changes if row.entity_type == "quote_observations"]
    assert len(quotes) == 3 and all(row.ledger_id is None for row in quotes)
    with pytest.raises(IntegrityError) as failure, engine.begin() as connection:
        connection.execute(text("DELETE FROM quote_observations"))
    assert failure.value.orig.diag.constraint_name == "ck_quote_observation_immutable"
    migration = (
        Path(__file__).resolve().parents[3]
        / "services/api/migrations/versions/20261125_0112_quote_observations.py"
    )
    with (
        pytest.raises(IntegrityError) as failure,
        engine.begin() as connection,
        Operations.context(MigrationContext.configure(connection)),
    ):
        runpy.run_path(str(migration))["downgrade"]()
    assert failure.value.orig.diag.constraint_name == "ck_quote_observation_downgrade"


def test_mobile_reads_distinct_identity_missing_manual_and_cached_observations(
    authenticated_client,
):
    client, engine, owner = authenticated_client
    now = datetime.now(UTC)
    observed = now - timedelta(days=2)
    prices = MarketQuoteService(engine)
    ticker = json.dumps({"price": "60000.001", "time": observed.isoformat()}).encode()
    observation = prices.refresh(owner, "BTC", "USD", fetch=lambda _: ticker, clock=lambda: now)
    _manual(client, observed.date(), "1.10")

    def page(assets, day):
        return client.get(
            QUOTES + "/effective",
            params=[
                ("display_asset_id", "USD"),
                ("price_date", day.isoformat()),
                *(("asset_ids", item) for item in assets),
            ],
        )

    result = page(["USD", "EUR", "BTC", "ETH"], observed.date())
    assert result.status_code == 200, result.text
    assert result.headers["cache-control"] == "no-store"
    rows = result.json()["items"]
    assert [(row["asset_id"], row["state"]) for row in rows] == [
        ("USD", "identity"),
        ("EUR", "manual"),
        ("BTC", "automatic"),
        ("ETH", "missing"),
    ]
    assert rows[2]["automatic_price"]["id"] == str(observation)
    assert rows[2]["automatic_price"]["price"] == "60000.001"
    assert datetime.fromisoformat(rows[2]["automatic_price"]["observed_at"]) == observed
    assert datetime.fromisoformat(rows[2]["automatic_price"]["fetched_at"]) == now
    assert page(["BTC"], now.date()).json()["items"][0]["state"] == "missing"
    assert page(["BTC", "BTC"], observed.date()).status_code == 422
    assert page(["ZZZ"], observed.date()).status_code == 404
    client.cookies.clear()
    assert page(["BTC"], observed.date()).status_code == 401
