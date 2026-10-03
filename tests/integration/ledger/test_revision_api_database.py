"""Real HTTP correction, cancellation, history and immutable receipt semantics."""

from uuid import uuid4

import pytest
from coinpup_api.ledger.schemas import AccountCreate, EntityCreate
from coinpup_api.ledger.service import LedgerService

pytestmark = pytest.mark.integration


def test_revision_http_lifecycle_keeps_stable_identity_and_terminal_state(authenticated_client):
    client, engine, owner = authenticated_client
    structure = LedgerService(engine)
    entity = structure.create_entity(
        owner,
        EntityCreate(
            kind="personal",
            name="Fictional revision HTTP ledger",
            base_asset_id="USD",
            template_key="personal_default",
        ),
    )
    ledger_id = entity.ledger.id
    account = structure.create_account(
        owner,
        ledger_id,
        AccountCreate(name="Fictional revision wallet", kind="wise", asset_ids=["USD", "ETH"]),
    )
    expense_category = next(
        item for item in structure.list_categories(owner, ledger_id) if item.kind == "expense"
    )
    path = f"/api/v1/ledgers/{ledger_id}"

    def post(suffix, body, key):
        return client.post(path + suffix, json=body, headers={"Idempotency-Key": key})

    def balances():
        return {item["asset_id"]: item["amount"] for item in client.get(path + "/balances").json()}

    for asset, amount in [("USD", "1000.00"), ("ETH", "1.000000000000000000")]:
        response = post(
            "/opening-balances",
            {
                "account_id": str(account.id),
                "asset_id": asset,
                "amount": amount,
                "transaction_date": "2026-08-01",
            },
            "opening-" + asset,
        )
        assert response.status_code == 201, response.text
    fee = {
        "account_id": str(account.id),
        "asset_id": "ETH",
        "amount": "0.000000000000000001",
        "category_id": str(expense_category.id),
    }
    expense = {
        "account_id": str(account.id),
        "asset_id": "USD",
        "amount": "100.00",
        "transaction_date": "2026-10-03",
        "recognition_date": "2026-09-30",
        "description": "Fictional original purchase",
        "splits": [{"category_id": str(expense_category.id), "amount": "100.00"}],
        "fees": [fee],
    }
    created = post("/expenses", expense, "create-expense")
    assert created.status_code == 201, created.text
    receipt = created.json()
    suffix = f"/operations/{receipt['id']}"
    assert balances() == {"USD": "900.00", "ETH": "0.999999999999999999"}

    replacement = expense | {
        "kind": "expense",
        "amount": "120.00",
        "recognition_date": "2026-09-29",
        "description": "Fictional corrected purchase",
        "splits": [{"category_id": str(expense_category.id), "amount": "120.00"}],
        "fees": [fee | {"amount": "0.000000000000000002"}],
    }
    correction = {
        "expected_version": 1,
        "reason": "Correct amount and fee",
        "replacement": replacement,
    }
    corrected = post(suffix + "/corrections", correction, "correct-expense")
    assert corrected.status_code == 200, corrected.text
    active = corrected.json()
    assert active["id"] == receipt["id"] and active["version"] == 2
    assert active["status"] == "active" and "cancellation" not in active
    assert active["latest_posting"]["amount"] == "120.00"
    assert active["latest_posting"]["version"] == 2
    assert balances() == {"USD": "880.00", "ETH": "0.999999999999999998"}
    assert client.get(path + suffix).json() == active
    assert post(suffix + "/corrections", correction, "correct-expense").json() == active
    assert post(suffix + "/corrections", correction, "stale-correction").status_code == 409

    # Invalid replacements and keys reused for another operation cannot change the graph.
    invalid = correction | {
        "expected_version": 2,
        "replacement": replacement | {"fees": [fee | {"category_id": str(uuid4())}]},
    }
    assert post(suffix + "/corrections", invalid, "invalid-replacement").status_code == 404
    assert (
        post(f"/operations/{uuid4()}/corrections", correction, "correct-expense").status_code == 409
    )
    assert client.get(path + suffix).json() == active
    assert len(client.get(path + suffix + "/history").json()) == 2

    # Old references remain reversible after archival and disabling the fee asset link.
    archived = client.patch(
        path + f"/accounts/{account.id}",
        json={"expected_version": 1, "archived": True, "asset_ids": ["USD"]},
    )
    assert archived.status_code == 200, archived.text
    category = client.patch(
        path + f"/categories/{expense_category.id}", json={"expected_version": 1, "archived": True}
    )
    assert category.status_code == 200, category.text
    cancellation = {"expected_version": 2, "reason": "Cancel fictional duplicate"}
    cancelled = post(suffix + "/cancellations", cancellation, "cancel-expense")
    assert cancelled.status_code == 200, cancelled.text
    terminal = cancelled.json()
    assert terminal["status"] == "cancelled" and terminal["version"] == 3
    assert terminal["latest_posting"] == active["latest_posting"]
    assert terminal["cancellation"]["version"] == 3
    assert terminal["cancellation"]["reason"] == cancellation["reason"]
    assert balances() == {"USD": "1000.00", "ETH": "1.000000000000000000"}
    assert client.get(path + suffix).json() == terminal
    assert post(suffix + "/cancellations", cancellation, "cancel-expense").json() == terminal
    assert post(suffix + "/corrections", correction, "correct-expense").json() == active
    assert post("/expenses", expense, "create-expense").json() == receipt
    assert (
        post(suffix + "/corrections", correction | {"expected_version": 3}, "revive").status_code
        == 409
    )
    assert (
        post(suffix + "/cancellations", cancellation | {"expected_version": 3}, "again").status_code
        == 409
    )
    assert client.get(path + suffix).json() == terminal

    all_records = client.get(path + "/operations").json()
    assert len(all_records) == 3
    assert client.get(path + "/operations?status=cancelled").json() == [terminal]
    assert len(client.get(path + "/operations?status=active").json()) == 2
    history = client.get(path + suffix + "/history").json()
    assert [entry["version"] for entry in history] == [1, 2, 3]
    assert [entry["action"] for entry in history] == ["create", "correct", "cancel"]
    assert all(entry["actor_id"] == str(owner) for entry in history)
    assert [item["kind"] for item in history[1]["journals"]] == ["reversal", "posting"]
    old = history[0]["journals"][0]
    reversal = history[1]["journals"][0]
    assert reversal["reverses_journal_id"] == old["id"]
    for field in ("transaction_date", "recognition_date", "description"):
        assert reversal[field] == old[field]
    reversal_fee = next(
        line
        for line in reversal["lines"]
        if line["component_no"] == 1 and line["role"] == "account"
    )
    assert reversal_fee["amount"] == "0.000000000000000001"
    assert client.get(path + suffix + "/history?limit=1&offset=1").json() == [history[1]]

    other = structure.create_entity(
        owner, EntityCreate(kind="personal", name="Fictional other", base_asset_id="USD")
    )
    other_path = f"/api/v1/ledgers/{other.ledger.id}" + suffix
    assert client.get(other_path).status_code == 404
    assert client.get(other_path + "/history").status_code == 404
    assert client.get(path + f"/operations/{uuid4()}/history").status_code == 404
