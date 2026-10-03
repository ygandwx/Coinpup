"""Real authenticated HTTP receipt replay, split accounting and exact asset reads."""

import os
from datetime import UTC, datetime, timedelta

import pytest
from coinpup_api.config import Settings
from coinpup_api.main import create_app
from coinpup_api.models import AuthSession
from coinpup_api.security import csrf_token_for, token_digest
from fastapi.testclient import TestClient
from sqlalchemy import func, update
from sqlalchemy.orm import Session

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("COINPUP_RUN_DB_TESTS") != "1",
        reason="Requires an explicitly configured disposable PostgreSQL database",
    ),
]


def test_http_exact_positions_split_expense_and_durable_replay(structure_database):
    engine, owner_id = structure_database

    class Probe:
        def __init__(self):
            self.engine = engine

        def check(self):
            pass

        def close(self):
            pass

    token = "fictional-posting-http-session"
    with Session(engine) as session, session.begin():
        session.add(
            AuthSession(
                token_hash=token_digest(token),
                administrator_id=owner_id,
                expires_at=datetime.now(UTC) + timedelta(hours=1),
            )
        )
    app = create_app(Settings(_env_file=None, environment="test"), Probe())
    with TestClient(app) as client:
        client.cookies.set("coinpup_session", token)
        client.headers.update(
            {"origin": "http://localhost:8000", "x-csrf-token": csrf_token_for(token)}
        )
        response = client.post(
            "/api/v1/entities",
            json={
                "kind": "personal",
                "name": "Fictional ledger",
                "base_asset_id": "USD",
                "template_key": "personal_default",
            },
        )
        assert response.status_code == 201, response.text
        entity = response.json()
        ledger = f"/api/v1/ledgers/{entity['ledger']['id']}"
        other = client.post(
            "/api/v1/entities",
            json={"kind": "personal", "name": "Fictional isolated ledger", "base_asset_id": "USD"},
        ).json()
        other_ledger = f"/api/v1/ledgers/{other['ledger']['id']}"
        response = client.post(
            ledger + "/accounts",
            json={
                "name": "Fictional multi-asset wallet",
                "kind": "wise",
                "asset_ids": ["USD", "EUR", "ETH"],
            },
        )
        assert response.status_code == 201, response.text
        account = response.json()
        categories = client.get(ledger + "/categories").json()
        expenses = [item for item in categories if item["kind"] == "expense"]
        income = next(item for item in categories if item["kind"] == "income")

        def post(path, body, key):
            return client.post(ledger + path, json=body, headers={"Idempotency-Key": key})

        for asset, amount in [("USD", "100.00"), ("EUR", "80.00"), ("ETH", "1.000000000000000001")]:
            body = {
                "account_id": account["id"],
                "asset_id": asset,
                "amount": amount,
                "transaction_date": "2026-09-01",
            }
            initial = post("/opening-balances", body, f"opening-{asset}")
            assert initial.status_code == 201, initial.text
            assert initial.json()["kind"] == "opening" and initial.json()["splits"] == []
            assert post("/opening-balances", body, f"opening-{asset}").json() == initial.json()
            assert post("/opening-balances", body, f"second-opening-{asset}").status_code == 409

        expense = {
            "account_id": account["id"],
            "asset_id": "USD",
            "amount": "25.00",
            "transaction_date": "2026-10-03",
            "recognition_date": "2026-09-30",
            "description": "Fictional split purchase",
            "splits": [
                {"category_id": expenses[0]["id"], "amount": "10.00"},
                {"category_id": expenses[1]["id"], "amount": "15.00"},
            ],
        }
        saved = post("/expenses", expense, "expense-1")
        assert saved.status_code == 201, saved.text
        receipt = saved.json()
        assert receipt["transaction_date"] == "2026-10-03"
        assert receipt["recognition_date"] == "2026-09-30"
        assert receipt["amount"] == "25.00" and len(receipt["splits"]) == 2
        assert post("/expenses", expense, "expense-1").json() == receipt
        assert client.get(ledger + f"/operations/{receipt['id']}").json() == receipt
        assert client.get(other_ledger + f"/operations/{receipt['id']}").status_code == 404
        assert client.get(other_ledger + "/operations").json() == []
        assert client.get(other_ledger + "/balances").json() == []
        changed = post("/expenses", expense | {"amount": "25"}, "expense-1")
        assert changed.status_code == 409
        assert changed.json()["detail"]["code"] == "idempotency_conflict"
        wrong_split = expense | {"splits": [{"category_id": expenses[0]["id"], "amount": "24.00"}]}
        assert post("/expenses", wrong_split, "bad-split").status_code == 422
        income_body = {
            "account_id": account["id"],
            "asset_id": "EUR",
            "amount": "10.00",
            "transaction_date": "2026-10-03",
            "recognition_date": "2026-10-03",
            "splits": [{"category_id": income["id"], "amount": "10.00"}],
        }
        received = post("/income", income_body, "income-1")
        assert received.status_code == 201, received.text
        balances = client.get(ledger + "/balances").json()
        assert {item["asset_id"]: item["amount"] for item in balances} == {
            "USD": "75.00",
            "EUR": "90.00",
            "ETH": "1.000000000000000001",
        }
        assert len(client.get(ledger + "/operations").json()) == 5
        assert len(client.get(ledger + "/operations?limit=2&offset=2").json()) == 2

        archived = client.patch(
            f"/api/v1/entities/{entity['id']}", json={"expected_version": 1, "archived": True}
        )
        assert archived.status_code == 200
        # Even after archival, the original successful request remains safely repeatable.
        assert post("/expenses", expense, "expense-1").json() == receipt
        assert post("/expenses", expense, "new-after-archive").status_code == 409
        assert client.get(ledger + "/balances").json() == balances
        with engine.begin() as connection:
            connection.execute(update(AuthSession).values(revoked_at=func.now()))
        assert post("/expenses", expense, "expense-1").status_code == 401
        assert client.get(ledger + "/balances").status_code == 401
