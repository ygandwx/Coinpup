"""Frozen pre-batch JSON, complete HTTP query budgets and current-posting dates."""

import json
import runpy
from pathlib import Path
from uuid import uuid4

import pytest
from coinpup_api.ledger.posting import PostingService
from coinpup_api.ledger.posting_schemas import (
    CancellationCreate,
    CorrectionCreate,
    ExchangeCreate,
    ExpenseCreate,
    IncomeCreate,
    OpeningCreate,
    TransferCreate,
)
from coinpup_api.ledger.schemas import (
    AccountCreate,
    AccountUpdate,
    AssetUpdate,
    CategoryCreate,
    CategoryUpdate,
    EntityCreate,
    EntityUpdate,
)
from coinpup_api.ledger.service import LedgerError, LedgerService
from sqlalchemy import event

pytestmark = pytest.mark.integration
CREATES = {
    "opening": OpeningCreate,
    "income": IncomeCreate,
    "expense": ExpenseCreate,
    "transfer": TransferCreate,
    "exchange": ExchangeCreate,
}


def frozen_reader(engine):
    module = runpy.run_path(str(Path(__file__).with_name("frozen_readers_opt12.py")))
    return module["FrozenReaderService"](engine)


def counted_get(client, engine, path, *, params=None, name, capsys):
    statements = []

    def record(connection, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    # Count the whole request, including authentication, owner and ledger/entity guards.
    event.listen(engine, "before_cursor_execute", record)
    try:
        response = client.get(path, params=params)
    finally:
        event.remove(engine, "before_cursor_execute", record)
    assert response.status_code == 200, response.text
    result = response.json()
    rows = len(result) if isinstance(result, list) else 1
    with capsys.disabled():
        print(
            "OPT12_QUERIES " + json.dumps({"case": name, "rows": rows, "queries": len(statements)})
        )
    return result, len(statements)


@pytest.fixture
def mixed_history(authenticated_client):
    client, engine, owner = authenticated_client
    structure, posting = LedgerService(engine), PostingService(engine)
    entity = structure.create_entity(
        owner,
        EntityCreate(kind="personal", name="Fictional batch / 虚构批量流水", base_asset_id="USD"),
    )
    ledger = entity.ledger.id
    categories = {
        kind: [
            structure.create_category(
                owner, ledger, CategoryCreate(name=f"Fictional {kind} {number}", kind=kind)
            )
            for number in range(2)
        ]
        for kind in ("income", "expense")
    }
    accounts = [
        structure.create_account(
            owner,
            ledger,
            AccountCreate(
                name=f"Fictional wallet {number}", kind="wise", asset_ids=["USD", "EUR", "ETH"]
            ),
        )
        for number in range(40)
    ]
    corrected, cancelled = set(), set()
    examples = {}
    for group, account in enumerate(accounts):
        day = f"2026-01-{group % 7 + 1:02d}"
        destination = accounts[(group + 1) % len(accounts)]
        metadata = {"transaction_date": day, "description": f"Fictional batch {group} / 虚构"}
        fees = [
            {
                "account_id": str(account.id),
                "asset_id": asset,
                "amount": amount,
                "category_id": str(categories["expense"][0].id),
            }
            for asset, amount in (("USD", "0.01"), ("ETH", "0.000000000000000001"))
        ]
        for kind, cls in CREATES.items():
            body = metadata.copy()
            if kind == "exchange":
                body.update(
                    source_account_id=str(account.id),
                    source_asset_id="USD",
                    source_amount="3.00",
                    destination_account_id=str(destination.id),
                    destination_asset_id="ETH",
                    destination_amount="0.000000000000000002",
                )
            else:
                body.update(asset_id="EUR" if kind == "income" else "USD")
                body["amount"] = "1000.00" if kind == "opening" else "2.00"
                if kind == "transfer":
                    body.update(
                        source_account_id=str(account.id),
                        destination_account_id=str(destination.id),
                    )
                else:
                    body["account_id"] = str(account.id)
                if kind in {"income", "expense"}:
                    body.update(
                        recognition_date=f"2025-12-{group % 7 + 1:02d}",
                        splits=[
                            {"category_id": str(category.id), "amount": "1.00"}
                            for category in categories[kind]
                        ],
                    )
            if kind != "opening" and group % 2 == 0:
                body["fees"] = fees
            receipt = getattr(posting, "post_" + kind)(
                owner, ledger, cls.model_validate(body), f"fictional-batch-{group}-{kind}"
            )
            expected_version = 1
            if group in {0, 1}:
                posting.correct_operation(
                    owner,
                    ledger,
                    receipt.id,
                    CorrectionCreate(
                        expected_version=1,
                        reason="Correct fictional posting date",
                        replacement=body
                        | {
                            "kind": kind,
                            "transaction_date": "2026-01-20",
                            "description": "Corrected / 更正",
                        },
                    ),
                    f"fictional-correct-{group}-{kind}",
                )
                corrected.add(str(receipt.id))
                expected_version = 2
            if group in {0, 2}:
                posting.cancel_operation(
                    owner,
                    ledger,
                    receipt.id,
                    CancellationCreate(
                        expected_version=expected_version, reason="Cancel fictional entry"
                    ),
                    f"fictional-cancel-{group}-{kind}",
                )
                cancelled.add(str(receipt.id))
            if group == 0:
                examples[kind] = str(receipt.id)

    # Different ledgers have real rows, so isolation cannot pass on empty data.
    def foreign_ledger(name):
        foreign = structure.create_entity(
            owner, EntityCreate(kind="personal", name=name, base_asset_id="USD")
        ).ledger.id
        account = structure.create_account(
            owner,
            foreign,
            AccountCreate(name="Fictional foreign wallet", kind="cash", asset_ids=["USD"]),
        )
        receipt = posting.post_opening(
            owner,
            foreign,
            OpeningCreate(
                account_id=account.id, asset_id="USD", amount="999", transaction_date="2026-01-20"
            ),
            "fictional-foreign-opening",
        )
        return foreign, str(receipt.id)

    other_ledger, other_operation = foreign_ledger("Fictional same-owner isolation")
    return {
        "client": client,
        "engine": engine,
        "owner": owner,
        "ledger": ledger,
        "structure": structure,
        "entity": entity,
        "accounts": accounts,
        "categories": categories,
        "corrected": corrected,
        "cancelled": cancelled,
        "examples": examples,
        "other_ledger": other_ledger,
        "other_operation": other_operation,
    }


def test_default_http_json_matches_frozen_reader_and_queries_stay_constant(mixed_history, capsys):
    s = mixed_history
    client, engine, owner, ledger = s["client"], s["engine"], s["owner"], s["ledger"]
    path = f"/api/v1/ledgers/{ledger}/operations"
    old = frozen_reader(engine)
    expected = [
        record.model_dump(mode="json") for record in old.list_operations(owner, ledger, limit=200)
    ]
    assert len(expected) == 200
    assert {row["kind"] for row in expected} == set(CREATES)
    assert len([row for row in expected if row["status"] == "cancelled"]) == 10
    default, _ = counted_get(client, engine, path, name="default_100", capsys=capsys)
    assert default == expected[:100]
    full, many = counted_get(
        client, engine, path, params={"limit": 200}, name="default_200", capsys=capsys
    )
    one, few = counted_get(
        client, engine, path, params={"limit": 1}, name="default_1", capsys=capsys
    )
    assert full == expected and one == expected[:1]
    assert few == many and 0 < many <= 8
    for status, limit, offset in (("all", 17, 7), ("active", 200, 0), ("cancelled", 200, 0)):
        baseline = [
            row.model_dump(mode="json")
            for row in old.list_operations(owner, ledger, status=status, limit=limit, offset=offset)
        ]
        result, count = counted_get(
            client,
            engine,
            path,
            params={"status": status, "limit": limit, "offset": offset},
            name=f"{status}_{limit}_{offset}",
            capsys=capsys,
        )
        assert result == baseline and count == many
    empty, count = counted_get(
        client, engine, path, params={"offset": 200}, name="empty_page", capsys=capsys
    )
    assert empty == [] and count == 5
    for kind, operation in s["examples"].items():
        baseline = next(row for row in expected if row["id"] == operation)
        single, count = counted_get(
            client, engine, path + "/" + operation, name="single_" + kind, capsys=capsys
        )
        assert single == baseline and 0 < count <= 8
        assert single["version"] == 3 and single["latest_posting"]["version"] == 2
        assert (
            single["cancellation"]["reversal_journal_id"] != single["latest_posting"]["journal_id"]
        )
    assert (
        client.get(
            f"/api/v1/ledgers/{s['other_ledger']}/operations/{s['examples']['expense']}"
        ).status_code
        == 404
    )
    assert client.get(path + "/" + s["other_operation"]).status_code == 404
    # The single-administrator product cannot create a second real owner. An unknown
    # service owner must still fail before reading this populated ledger.
    with pytest.raises(LedgerError) as rejected:
        PostingService(engine).list_operations(uuid4(), ledger, order="transaction_date")
    assert rejected.value.status == 404
    assert client.get(f"/api/v1/ledgers/{uuid4()}/operations").status_code == 404

    s["structure"].update_account(
        owner,
        ledger,
        s["accounts"][0].id,
        AccountUpdate(expected_version=1, archived=True, asset_ids=["USD"]),
    )
    s["structure"].update_category(
        owner,
        ledger,
        s["categories"]["expense"][0].id,
        CategoryUpdate(expected_version=1, archived=True),
    )
    s["structure"].update_asset(owner, "ETH", AssetUpdate(expected_version=1, enabled=False))
    s["structure"].update_entity(
        owner, s["entity"].id, EntityUpdate(expected_version=1, archived=True)
    )
    archived, count = counted_get(
        client, engine, path, params={"limit": 200}, name="archived_disabled_200", capsys=capsys
    )
    assert archived == expected and count == many
    assert [
        row.model_dump(mode="json") for row in old.list_operations(owner, ledger, limit=200)
    ] == expected


def test_date_filters_use_current_posting_closed_bounds_and_original_tie_order(
    mixed_history, capsys
):
    s = mixed_history
    client, owner, ledger = s["client"], s["owner"], s["ledger"]
    path = f"/api/v1/ledgers/{ledger}/operations"
    baseline = [
        row.model_dump(mode="json")
        for row in frozen_reader(s["engine"]).list_operations(owner, ledger, limit=200)
    ]
    # Stable Python sorting preserves the frozen created_at/id order within each date.
    dated = sorted(
        baseline, key=lambda row: row["latest_posting"]["transaction_date"], reverse=True
    )
    cases = [
        {"order": "transaction_date"},
        {"order": "created_at", "from_date": "2026-01-01", "to_date": "2026-01-07"},
        {"order": "transaction_date", "from_date": "2026-01-20", "to_date": "2026-01-20"},
        {"order": "transaction_date", "from_date": "2026-01-03"},
        {"order": "transaction_date", "to_date": "2026-01-03"},
        {
            "order": "transaction_date",
            "status": "cancelled",
            "from_date": "2026-01-03",
            "to_date": "2026-01-03",
        },
        {"order": "transaction_date", "limit": 17, "offset": 8},
    ]
    for number, params in enumerate(cases):
        ordered = dated if params["order"] == "transaction_date" else baseline
        expected = [
            row
            for row in ordered
            if ("status" not in params or row["status"] == params["status"])
            and (
                "from_date" not in params
                or row["latest_posting"]["transaction_date"] >= params["from_date"]
            )
            and (
                "to_date" not in params
                or row["latest_posting"]["transaction_date"] <= params["to_date"]
            )
        ]
        offset, limit = params.get("offset", 0), params.get("limit", 200)
        result, count = counted_get(
            client,
            s["engine"],
            path,
            params={"limit": 200} | params,
            name=f"dated_page_{number}",
            capsys=capsys,
        )
        assert result == expected[offset : offset + limit]
        assert 0 < count <= 8
    corrected_date = client.get(
        path, params={"limit": 200, "from_date": "2026-01-20", "to_date": "2026-01-20"}
    ).json()
    assert {row["id"] for row in corrected_date} == s["corrected"]
    old_dates = client.get(path, params={"limit": 200, "to_date": "2026-01-07"}).json()
    assert s["corrected"].isdisjoint(row["id"] for row in old_dates)
    assert client.get(path, params={"from_date": "2030-01-01"}).json() == []
    for params in (
        {"order": "unknown"},
        {"from_date": "2026-1-02"},
        {"to_date": "2026-02-30"},
        {"from_date": "20260102"},
        {"to_date": "2026-01-02T00:00:00Z"},
        {"from_date": "2026-01-20", "to_date": "2026-01-01"},
    ):
        assert client.get(path, params=params).status_code == 422


def test_transaction_date_order_uses_id_when_original_creation_times_are_equal(
    authenticated_client,
):
    client, engine, owner = authenticated_client
    structure = LedgerService(engine)
    ledger = structure.create_entity(
        owner, EntityCreate(kind="personal", name="Fictional tied timestamps", base_asset_id="USD")
    ).ledger.id
    account = structure.create_account(
        owner, ledger, AccountCreate(name="Fictional tie wallet", kind="cash", asset_ids=["USD"])
    )
    category = structure.create_category(
        owner, ledger, CategoryCreate(name="Fictional tie expense", kind="expense")
    )
    fixture = {
        "owner": owner,
        "ledger": ledger,
        "accounts": [account.id],
        "categories": [category.id],
    }
    initial = runpy.run_path(str(Path(__file__).with_name("test_revision_constraints.py")))[
        "_initial"
    ]
    # All server now() values in this real transaction are equal, without changing sealed rows.
    with engine.begin() as connection:
        operations = [str(initial(connection, fixture)[0]) for _ in range(3)]
    baseline = [
        row.model_dump(mode="json") for row in frozen_reader(engine).list_operations(owner, ledger)
    ]
    assert len({row["latest_posting"]["created_at"] for row in baseline}) == 1
    response = client.get(
        f"/api/v1/ledgers/{ledger}/operations", params={"order": "transaction_date"}
    )
    assert response.status_code == 200, response.text
    assert response.json() == baseline
    assert [row["id"] for row in response.json()] == sorted(operations, reverse=True)
