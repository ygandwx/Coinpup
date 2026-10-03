"""Real HTTP account transfers and card settlement preserve financial classification."""

import os
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from coinpup_api.config import Settings
from coinpup_api.ledger.models import JournalLine
from coinpup_api.ledger.schemas import AccountCreate, AccountUpdate, EntityCreate
from coinpup_api.ledger.service import LedgerService
from coinpup_api.main import create_app
from coinpup_api.models import AuthSession
from coinpup_api.security import csrf_token_for, token_digest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("COINPUP_RUN_DB_TESTS") != "1",
        reason="Requires an explicitly configured disposable PostgreSQL database",
    ),
]


def test_http_transfers_card_repayment_and_original_receipt_shapes(structure_database):
    engine, owner = structure_database
    structure = LedgerService(engine)
    entity = structure.create_entity(
        owner,
        EntityCreate(
            kind="personal",
            name="Fictional transfers",
            base_asset_id="USD",
            template_key="personal_default",
        ),
    )
    ledger_id = entity.ledger.id
    accounts = {
        kind: structure.create_account(
            owner, ledger_id, AccountCreate(name="Fictional " + kind, kind=kind, asset_ids=["USD"])
        )
        for kind in ("bank", "cash", "credit_card")
    }
    category = next(
        item for item in structure.list_categories(owner, ledger_id) if item.kind == "expense"
    )
    token = "fictional-transfer-http-session"
    with Session(engine) as session, session.begin():
        session.add(
            AuthSession(
                token_hash=token_digest(token),
                administrator_id=owner,
                expires_at=datetime.now(UTC) + timedelta(hours=1),
            )
        )

    class Probe:
        def __init__(self):
            self.engine = engine

        def check(self):
            pass

        def close(self):
            pass

    path = f"/api/v1/ledgers/{ledger_id}"
    with TestClient(create_app(Settings(_env_file=None, environment="test"), Probe())) as client:
        client.cookies.set("coinpup_session", token)
        client.headers.update(
            {"origin": "http://localhost:8000", "x-csrf-token": csrf_token_for(token)}
        )

        def post(suffix, body, key):
            return client.post(path + suffix, json=body, headers={"Idempotency-Key": key})

        opening = {
            "account_id": str(accounts["bank"].id),
            "asset_id": "USD",
            "amount": "1000.00",
            "transaction_date": "2026-10-01",
        }
        initial = post("/opening-balances", opening, "bank-opening")
        assert initial.status_code == 201, initial.text
        assert "destination_account_id" not in initial.json()
        transfer = {
            "source_account_id": str(accounts["bank"].id),
            "destination_account_id": str(accounts["cash"].id),
            "asset_id": "USD",
            "amount": "200.00",
            "transaction_date": "2026-10-02",
            "description": "Fictional cash withdrawal",
        }
        moved = post("/transfers", transfer, "cash-transfer")
        assert moved.status_code == 201, moved.text
        receipt = moved.json()
        assert receipt["kind"] == "transfer" and receipt["amount"] == "200.00"
        assert receipt["source_account_id"] == str(accounts["bank"].id)
        assert receipt["destination_account_id"] == str(accounts["cash"].id)
        assert "splits" not in receipt and "account_id" not in receipt
        assert receipt["recognition_date"] == receipt["transaction_date"]
        assert post("/transfers", transfer, "cash-transfer").json() == receipt
        state = client.get(path + f"/operations/{receipt['id']}").json()
        assert state["latest_posting"] == receipt
        assert state["status"] == "active" and state["version"] == 1
        assert (
            post("/transfers", transfer | {"amount": "201.00"}, "cash-transfer").status_code == 409
        )
        assert (
            post(
                "/transfers",
                transfer | {"destination_account_id": transfer["source_account_id"]},
                "self-transfer",
            ).status_code
            == 422
        )
        assert (
            post(
                "/transfers",
                transfer | {"destination_account_id": str(uuid4())},
                "missing-destination",
            ).status_code
            == 404
        )

        expense = {
            "account_id": str(accounts["credit_card"].id),
            "asset_id": "USD",
            "amount": "100.00",
            "transaction_date": "2026-10-02",
            "recognition_date": "2026-10-02",
            "splits": [{"category_id": str(category.id), "amount": "100.00"}],
        }
        paid = post("/expenses", expense, "card-purchase")
        assert paid.status_code == 201, paid.text
        before_repayment = {
            item["account_id"]: item["amount"] for item in client.get(path + "/balances").json()
        }
        assert before_repayment[str(accounts["credit_card"].id)] == "-100.00"
        repayment = transfer | {
            "destination_account_id": str(accounts["credit_card"].id),
            "amount": "100.00",
            "description": "Fictional card repayment",
        }
        repaid = post("/transfers", repayment, "card-repayment")
        assert repaid.status_code == 201, repaid.text
        balances = {
            item["account_id"]: item["amount"] for item in client.get(path + "/balances").json()
        }
        assert balances == {
            str(accounts["bank"].id): "700.00",
            str(accounts["cash"].id): "200.00",
            str(accounts["credit_card"].id): "0.00",
        }
        operations = client.get(path + "/operations").json()
        assert len(operations) == 4
        assert {item["kind"] for item in operations} == {"opening", "expense", "transfer"}
        assert all(item["status"] == "active" and item["version"] == 1 for item in operations)
        assert (
            next(item["latest_posting"] for item in operations if item["id"] == receipt["id"])
            == receipt
        )
        assert post("/opening-balances", opening, "bank-opening").json() == initial.json()
        assert post("/expenses", expense, "card-purchase").json() == paid.json()

        structure.update_account(
            owner, ledger_id, accounts["cash"].id, AccountUpdate(expected_version=1, archived=True)
        )
        assert post("/transfers", transfer, "cash-transfer").json() == receipt
        assert post("/transfers", transfer, "new-archived-transfer").status_code == 409
        assert len(client.get(path + "/operations").json()) == 4
        with engine.connect() as connection:
            assert connection.scalar(
                select(func.sum(JournalLine.amount)).where(
                    JournalLine.ledger_id == ledger_id, JournalLine.role == "expense"
                )
            ) == Decimal("100.00")
            assert (
                connection.scalar(
                    select(func.count())
                    .select_from(JournalLine)
                    .where(JournalLine.ledger_id == ledger_id, JournalLine.role == "income")
                )
                == 0
            )
