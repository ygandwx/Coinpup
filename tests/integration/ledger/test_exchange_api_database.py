"""Actual exchange and fee quantities survive HTTP, replay and accounting reads."""

import os
from decimal import Decimal
from uuid import uuid4

import pytest
from coinpup_api.ledger.models import JournalLine
from coinpup_api.ledger.schemas import AccountCreate, EntityCreate
from coinpup_api.ledger.service import LedgerService
from sqlalchemy import func, select

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("COINPUP_RUN_DB_TESTS") != "1",
        reason="Requires an explicitly configured disposable PostgreSQL database",
    ),
]


def test_http_exchange_and_crypto_payment_keep_fees_separate(
    authenticated_client, legacy_v1_receipts
):
    client, engine, owner = authenticated_client
    structure = LedgerService(engine)
    entity = structure.create_entity(
        owner,
        EntityCreate(
            kind="personal",
            name="Fictional exchange",
            base_asset_id="USD",
            template_key="personal_default",
        ),
    )
    ledger_id = entity.ledger.id
    account = structure.create_account(
        owner,
        ledger_id,
        AccountCreate(name="Fictional wallet", kind="wise", asset_ids=["USD", "EUR", "BTC"]),
    )
    categories = [
        item for item in structure.list_categories(owner, ledger_id) if item.kind == "expense"
    ]
    path = f"/api/v1/ledgers/{ledger_id}"

    def post(suffix, body, key):
        return client.post(path + suffix, json=body, headers={"Idempotency-Key": key})

    def balances():
        return {item["asset_id"]: item["amount"] for item in client.get(path + "/balances").json()}

    for asset, amount in [("USD", "1000.00"), ("BTC", "1.00000000")]:
        body = {
            "account_id": str(account.id),
            "asset_id": asset,
            "amount": amount,
            "transaction_date": "2026-10-01",
        }
        response = post("/opening-balances", body, "opening-" + asset)
        assert response.status_code == 201, response.text
        assert "fees" not in response.json()
    fee = {
        "account_id": str(account.id),
        "asset_id": "USD",
        "amount": "2.00",
        "category_id": str(categories[0].id),
    }
    exchange = {
        "source_account_id": str(account.id),
        "source_asset_id": "USD",
        "source_amount": "100.00",
        "destination_account_id": str(account.id),
        "destination_asset_id": "EUR",
        "destination_amount": "90.00",
        "transaction_date": "2026-10-02",
        "fees": [fee],
    }
    converted = post("/exchanges", exchange, "exchange-1")
    assert converted.status_code == 201, converted.text
    receipt = converted.json()
    assert receipt["kind"] == "exchange"
    assert receipt["source_amount"] == "100.00" and receipt["destination_amount"] == "90.00"
    assert receipt["fees"] == [fee]
    assert post("/exchanges", exchange, "exchange-1").json() == receipt
    state = client.get(path + f"/operations/{receipt['id']}").json()
    assert state["status"] == "active" and state["version"] == 1
    assert state["latest_posting"] == receipt
    assert balances() == {"USD": "898.00", "EUR": "90.00", "BTC": "1.00000000"}
    assert (
        post(
            "/exchanges", exchange | {"fees": [fee | {"amount": "3.00"}]}, "exchange-1"
        ).status_code
        == 409
    )

    invalid_fee = fee | {"category_id": str(uuid4())}
    failed = post("/exchanges", exchange | {"fees": [fee, invalid_fee]}, "invalid-last-fee")
    assert failed.status_code == 404
    assert balances() == {"USD": "898.00", "EUR": "90.00", "BTC": "1.00000000"}
    assert len(client.get(path + "/operations").json()) == 3

    btc_fee = fee | {"asset_id": "BTC", "amount": "0.00001000"}
    purchase = {
        "account_id": str(account.id),
        "asset_id": "BTC",
        "amount": "0.10000000",
        "transaction_date": "2026-10-03",
        "recognition_date": "2026-10-03",
        "splits": [{"category_id": str(categories[1].id), "amount": "0.10000000"}],
        "fees": [btc_fee],
    }
    paid = post("/expenses", purchase, "btc-payment")
    assert paid.status_code == 201, paid.text
    assert paid.json()["amount"] == "0.10000000" and paid.json()["fees"] == [btc_fee]
    assert post("/expenses", purchase, "btc-payment").json() == paid.json()
    assert balances()["BTC"] == "0.89999000"
    records = client.get(path + "/operations").json()
    assert (
        next(item for item in records if item["id"] == paid.json()["id"])["latest_posting"]
        == paid.json()
    )
    assert (
        next(item for item in records if item["id"] == receipt["id"])["latest_posting"] == receipt
    )
    with engine.connect() as connection:
        for asset, expected in [("USD", Decimal("2.00")), ("BTC", Decimal("0.10001"))]:
            assert (
                connection.scalar(
                    select(func.sum(JournalLine.amount)).where(
                        JournalLine.ledger_id == ledger_id,
                        JournalLine.asset_id == asset,
                        JournalLine.role == "expense",
                    )
                )
                == expected
            )

    # Adding an explicit empty fee list remains compatible with the original fee-free command.
    legacy = legacy_v1_receipts["cases"]["expense"]
    path = f"/api/v1/ledgers/{legacy_v1_receipts['ledger']}"
    no_fee = legacy["body"] | {"fees": []}
    no_fee.pop("fees")
    original = post("/expenses", no_fee, legacy["key"])
    assert original.status_code == 201, original.text
    assert "fees" not in original.json()
    assert post("/expenses", no_fee | {"fees": []}, legacy["key"]).json() == original.json()
    assert (
        client.get(path + f"/operations/{original.json()['id']}").json()["latest_posting"]
        == original.json()
    )
