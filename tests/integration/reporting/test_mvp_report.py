"""Exercise owner-scoped quoted reports and sealed financial facts on PostgreSQL."""

from uuid import uuid4

import pytest
from coinpup_api.ledger.posting_schemas import OpeningCreate
from coinpup_api.ledger.schemas import AccountCreate
from coinpup_api.ledger.service import LedgerService
from coinpup_api.settlement.schemas import InterledgerRepaymentCreate
from coinpup_api.sync.service import ChangeService
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from tests.integration.settlement.test_interledger import _create
from tests.integration.settlement.test_interledger import pair as pair

pytestmark = pytest.mark.integration
QUOTES = "/api/v1/quotes/manual"
REPORTS = "/api/v1/reports/mvp"
PRICE = {
    "asset_id": "USD",
    "display_asset_id": "EUR",
    "price_date": "2026-11-10",
    "price": "1.123456789123456789",
    "source": "Fictional test source",
    "expected_version": 0,
}


def _report(client, view, *scope, expected=200, **params):
    response = client.get(
        REPORTS,
        params=[("view", view), *(("ledger_ids", ledger) for ledger in scope), *params.items()],
    )
    assert response.status_code == expected, response.text
    return response.json()


def test_price_updates_are_exact_owner_scoped_and_replay_safe(authenticated_client):
    client, engine, owner = authenticated_client
    token = client.headers.pop("x-csrf-token")
    assert client.put(QUOTES, json=PRICE).status_code == 403
    client.headers["x-csrf-token"] = token
    first = client.put(QUOTES, json=PRICE)
    assert first.status_code == 200, first.text
    saved = first.json()
    assert saved["price"] == PRICE["price"] and saved["version"] == 1
    assert client.put(QUOTES, json=PRICE).json() == saved
    assert client.put(QUOTES, json=PRICE | {"price": "1.10"}).status_code == 409
    second = client.put(QUOTES, json=PRICE | {"expected_version": 1, "price": "1.10"})
    assert second.status_code == 200, second.text
    updated = second.json()
    assert updated["id"] == saved["id"] and updated["version"] == 2
    assert updated["price"] == "1.10"
    assert (
        client.put(QUOTES, json=PRICE | {"expected_version": 1, "price": "1.10"}).json() == updated
    )
    assert client.put(QUOTES, json=PRICE).status_code == 409
    query = {"price_date": PRICE["price_date"], "display_asset_id": "EUR"}
    assert client.get(QUOTES, params=query | {"asset_id": "USD"}).json()["items"] == [updated]
    assert client.get(QUOTES, params=query | {"asset_id": "GBP"}).json()["items"] == []
    changes = ChangeService(engine).list_changes(owner, limit=200).changes
    price_changes = [row for row in changes if row.entity_type == "manual_prices"]
    assert [(row.entity_id, row.entity_version, row.ledger_id) for row in price_changes] == [
        (saved["id"], 1, None),
        (saved["id"], 2, None),
    ]
    with engine.begin() as connection:
        assert connection.execute(
            text("SELECT price, source FROM manual_price_audits ORDER BY version")
        ).all() == [(PRICE["price"], PRICE["source"]), ("1.10", PRICE["source"])]
    with pytest.raises(IntegrityError) as caught:
        with engine.begin() as connection:
            connection.execute(
                text("UPDATE manual_price_audits SET price = '2.00' WHERE version = 1")
            )
    assert caught.value.orig.diag.constraint_name == "ck_manual_price_audit"


@pytest.mark.parametrize(
    "changes",
    [
        {"price": 1.125},
        {"price": "0.0"},
        {"price": "1e-3"},
        {"price": "1.0000000000000000001"},
        {"price": "١.١"},
        {"price_date": "2026-02-29"},
        {"source": "  Fictional  "},
        {"display_asset_id": "USD"},
    ],
)
def test_rejects_invalid_manual_price_at_http_boundary(authenticated_client, changes):
    client, _, _ = authenticated_client
    assert client.put(QUOTES, json=PRICE | changes).status_code == 422


def test_cash_reports_eliminate_only_both_linked_repayment_sides(pair, authenticated_client):
    client, _, _ = authenticated_client
    payer, beneficiary = pair["payer"], pair["beneficiary"]
    advance = _create(pair)
    receipt = pair["service"].repay(
        pair["owner"],
        advance.id,
        InterledgerRepaymentCreate(**(pair["repayment"] | {"amount": "40.01"})),
        "fictional-first-repayment",
    )
    report = _report(client, "cash", payer, beneficiary)
    assert report["originals"] == [
        {"asset_id": "USD", "amount": "-100.00", "inflow": "0.00", "outflow": "100.00"}
    ]
    assert [(row["amount"], row["repayment_group_id"]) for row in report["rows"][:1]] == [
        ("-100.00", None)
    ]
    repaid = report["rows"][1:]
    assert {row["amount"] for row in repaid} == {"40.01", "-40.01"}
    assert all(
        row["repayment_group_id"] == row["excluded_repayment_group"] == str(receipt.id)
        for row in repaid
    )
    assert all(row["valuation_state"] == "excluded" for row in repaid)
    single = _report(client, "cash", payer)
    assert single["originals"][0]["amount"] == "-59.99"
    assert single["rows"][1]["repayment_group_id"] == str(receipt.id)
    assert single["rows"][1]["excluded_repayment_group"] is None
    operating = _report(client, "operating", payer, beneficiary)
    assert operating["originals"] == report["originals"]
    assert len(operating["rows"]) == 1
    assert operating["rows"][0]["ledger_id"] == str(beneficiary)
    page = _report(client, "cash", payer, beneficiary, limit=1)
    assert len(page["rows"]) == 1 and page["total_rows"] == 3
    assert page["originals"] == report["originals"]
    cutoff = _report(client, "cash", payer, beneficiary, to_date="2026-11-02")
    assert cutoff["total_rows"] == 1 and cutoff["originals"] == page["originals"]


def test_historical_balances_keep_original_units_and_explain_missing_quote(
    pair, authenticated_client
):
    client, engine, owner = authenticated_client
    payer = pair["payer"]
    _create(pair)
    account = LedgerService(engine).create_account(
        owner, payer, AccountCreate(name="Fictional euro bank", kind="bank", asset_ids=["EUR"])
    )
    pair["posting"].post_opening(
        owner,
        payer,
        OpeningCreate(
            account_id=account.id, asset_id="EUR", amount="50.25", transaction_date="2026-11-01"
        ),
        "fictional-euro-opening",
    )
    report = _report(
        client,
        "balances",
        payer,
        to_date="2026-11-01",
        price_date=PRICE["price_date"],
        display_asset_id="EUR",
    )
    assert [(row["asset_id"], row["amount"]) for row in report["originals"]] == [
        ("EUR", "50.25"),
        ("USD", "1000.00"),
    ]
    assert report["valuation"] == {
        "display_asset_id": "EUR",
        "price_date": PRICE["price_date"],
        "amount": None,
        "missing_asset_ids": ["USD"],
        "missing_rows": 1,
    }
    quote = client.put(QUOTES, json=PRICE | {"price": "1.10"}).json()
    current = _report(
        client, "balances", payer, price_date=PRICE["price_date"], display_asset_id="EUR"
    )
    assert current["valuation"]["amount"] == "1040.25"
    assert {row["valuation_state"] for row in current["rows"]} == {"identity", "quoted"}
    assert next(row["price"] for row in current["rows"] if row["asset_id"] == "USD") == quote
    wrong_date = _report(client, "balances", payer, price_date="2026-11-11", display_asset_id="EUR")
    assert wrong_date["valuation"]["amount"] is None
    assert wrong_date["valuation"]["missing_asset_ids"] == ["USD"]
    _report(client, "balances", payer, payer, expected=422)
    _report(client, "balances", payer, uuid4(), expected=404)
    _report(client, "balances", payer, from_date="2026-11-01", expected=422)
    _report(client, "cash", payer, price_date=PRICE["price_date"], expected=422)
