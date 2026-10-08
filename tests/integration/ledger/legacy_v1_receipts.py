"""Commit real v1 history at migration 0008 before upgrading to the current schema."""

import hashlib
import json
import runpy
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

from alembic import command
from alembic.config import Config
from coinpup_api.ledger.models import FinancialOperation, OpeningPosition
from coinpup_api.ledger.posting import PostingService
from coinpup_api.ledger.readers import PostingReaders, operation_state
from coinpup_api.ledger.schemas import CategoryCreate, EntityCreate
from coinpup_api.ledger.service import LedgerService
from sqlalchemy import MetaData, Table, insert, inspect, select, text

from tests.integration.ledger.legacy_v1_views import (
    DIMENSIONS,
    historical_line_model,
    historical_reader_session,
)

ROOT = Path(__file__).resolve().parents[3]
FINANCIAL_TABLES = (
    "financial_operations",
    "journals",
    "journal_lines",
    "opening_positions",
    "command_receipts",
)


def financial_snapshot(engine):
    """Compare complete immutable data across the added version column and replays."""
    with engine.connect() as connection:
        result = {}
        for name in FINANCIAL_TABLES:
            table = Table(name, MetaData(), autoload_with=connection)
            rows = connection.execute(select(table).order_by(*table.primary_key.columns)).mappings()
            result[name] = []
            for row in rows:
                if name == "journal_lines":
                    assert all(row.get(key) is None for key in DIMENSIONS)
                result[name].append(
                    {
                        key: value
                        for key, value in row.items()
                        if key != "hash_version" and key not in DIMENSIONS
                    }
                )
        return result


def _old_creation(kind, body, *, replacement=False):
    """Literal 0008 request projection, independent of today's schemas/hash code."""
    value = {
        "id": body.get("id"),
        "transaction_date": body["transaction_date"],
        "description": body.get("description", ""),
    }
    fields = (
        (
            "source_account_id",
            "source_asset_id",
            "source_amount",
            "destination_account_id",
            "destination_asset_id",
            "destination_amount",
        )
        if kind == "exchange"
        else ("asset_id", "amount", "source_account_id", "destination_account_id")
        if kind == "transfer"
        else ("asset_id", "amount", "account_id")
    )
    value.update({field: body[field] for field in fields})
    if kind in {"income", "expense"}:
        value["recognition_date"] = body["recognition_date"]
        value["splits"] = [
            {field: item[field] for field in ("category_id", "amount")} for item in body["splits"]
        ]
    if kind != "opening" and (kind == "exchange" or replacement or body.get("fees")):
        value["fees"] = [
            {field: fee[field] for field in ("account_id", "asset_id", "amount", "category_id")}
            for fee in body.get("fees", [])
        ]
    if replacement:
        value.pop("id")
        value["kind"] = kind
    return value


def _old_hash(action, ledger, body, operation=None):
    if operation is None:
        value = {"kind": action, "ledger_id": str(ledger), "payload": _old_creation(action, body)}
    else:
        payload = {"expected_version": body["expected_version"], "reason": body["reason"].strip()}
        if action == "correct":
            replacement = body["replacement"]
            payload["replacement"] = _old_creation(
                replacement["kind"], replacement, replacement=True
            )
        value = {
            "action": action,
            "ledger_id": str(ledger),
            "operation_id": str(operation),
            "payload": payload,
        }
    canonical = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


def create_legacy_v1_receipts(engine, owner):
    """Only the opted-in, already-cleaned fixture database may run this migration cycle."""
    config = Config(str(ROOT / "alembic.ini"))
    before = financial_snapshot(engine)
    assert all(not rows for rows in before.values()), (
        "Downgrade only before financial history exists"
    )
    cases = {}
    try:
        command.downgrade(config, "20261003_0008")
        with engine.connect() as connection:
            assert (
                connection.scalar(text("SELECT version_num FROM alembic_version"))
                == "20261003_0008"
            )
            receipt_table = Table("command_receipts", MetaData(), autoload_with=connection)
            assert "hash_version" not in receipt_table.c

        service = LedgerService(engine)
        entity = service.create_entity(
            owner, EntityCreate(kind="personal", name="Fictional v1 history", base_asset_id="USD")
        )
        ledger = entity.ledger.id
        # Seed actual 0008 columns; current ORM account classes did not exist then.
        accounts = [SimpleNamespace(id=uuid4()) for _ in range(2)]
        with engine.begin() as connection:
            metadata = MetaData()
            account_table = Table("accounts", metadata, autoload_with=connection)
            account_assets = Table("account_assets", metadata, autoload_with=connection)
            assert "account_class" not in account_table.c
            for index, account in enumerate(accounts):
                connection.execute(
                    insert(account_table).values(
                        id=account.id,
                        ledger_id=ledger,
                        name=f"Fictional historical wallet {index}",
                        kind="wise",
                    )
                )
                for asset in ("USD", "EUR"):
                    connection.execute(
                        insert(account_assets).values(
                            account_id=account.id,
                            ledger_id=ledger,
                            asset_id=asset,
                        )
                    )
        expense = service.create_category(
            owner, ledger, CategoryCreate(name="Fictional v1 expense", kind="expense")
        )
        income = service.create_category(
            owner, ledger, CategoryCreate(name="Fictional v1 income", kind="income")
        )
        fixture = {
            "owner": owner,
            "ledger": ledger,
            "accounts": [item.id for item in accounts],
            "categories": [expense.id],
            "income": income.id,
        }
        exchange = runpy.run_path(str(Path(__file__).with_name("test_exchange_constraints.py")))
        revisions = runpy.run_path(str(Path(__file__).with_name("test_revision_constraints.py")))
        old_line = historical_line_model(engine)
        # Scope the old schema to this runpy fixture namespace, never the application mapper.
        revisions["_initial"].__globals__["JournalLine"] = old_line
        revisions["_revise"].__globals__["JournalLine"] = old_line
        fee = {
            "account_id": str(accounts[0].id),
            "asset_id": "USD",
            "amount": "2",
            "category_id": str(expense.id),
        }

        def remember(name, action, operation, body, suffix, *, status=201):
            with historical_reader_session(engine) as session:
                record = session.get(FinancialOperation, operation)
                response = (
                    PostingReaders._read_operation(session, record)
                    if status == 201
                    else operation_state(session, record)
                ).model_dump(mode="json")
            digest = _old_hash(action, ledger, body, operation if status == 200 else None)
            key = "legacy-empty" if name == "expense" else "legacy-" + name
            with engine.begin() as connection:
                # This reflected table has no version column: no new-service receipt or UPDATE.
                connection.execute(
                    insert(receipt_table).values(
                        ledger_id=ledger,
                        key=key,
                        request_hash=digest,
                        response=response,
                        response_status=status,
                        operation_id=operation,
                    )
                )
            cases[name] = {
                "body": body,
                "key": key,
                "response": response,
                "status": status,
                "hash": digest,
                "suffix": suffix,
            }

        for kind, suffix in (
            ("opening", "/opening-balances"),
            ("income", "/income"),
            ("expense", "/expenses"),
            ("transfer", "/transfers"),
            ("exchange", "/exchanges"),
        ):
            body = {"transaction_date": "2026-01-01"}
            if kind == "exchange":
                body.update(
                    source_account_id=str(accounts[0].id),
                    source_asset_id="USD",
                    source_amount="100",
                    destination_account_id=str(accounts[1].id),
                    destination_asset_id="EUR",
                    destination_amount="90",
                    fees=[fee],
                )
            else:
                body.update(asset_id="USD", amount="100")
                if kind == "transfer":
                    body.update(
                        source_account_id=str(accounts[0].id),
                        destination_account_id=str(accounts[1].id),
                        fees=[fee],
                    )
                else:
                    body["account_id"] = str(accounts[0].id)
                if kind in {"income", "expense"}:
                    body.update(
                        recognition_date="2026-01-01",
                        splits=[
                            {
                                "category_id": str(income.id if kind == "income" else expense.id),
                                "amount": "100",
                            }
                        ],
                    )
                if kind == "income":
                    body["fees"] = [fee]
            with engine.begin() as connection:
                operation, journal = exchange["_header"](connection, fixture, kind)
                rows = (
                    exchange["_principal"](fixture, journal)
                    if kind == "exchange"
                    else exchange["_legacy_principal"](fixture, journal, kind)
                )
                if body.get("fees"):
                    rows += exchange["_fee"](fixture, journal, start=len(rows) + 1)
                connection.execute(insert(old_line), rows)
                if kind == "opening":
                    connection.execute(
                        insert(OpeningPosition).values(
                            account_id=accounts[0].id,
                            asset_id="USD",
                            ledger_id=ledger,
                            operation_id=operation,
                        )
                    )
            remember(kind, kind, operation, body, suffix)

        with engine.begin() as connection:
            operation, _ = revisions["_initial"](connection, fixture, fees=True)
        body = {
            "account_id": str(accounts[0].id),
            "asset_id": "USD",
            "amount": "100",
            "transaction_date": "2026-01-01",
            "recognition_date": "2026-01-01",
            "description": "Original fictional entry",
            "splits": [{"category_id": str(expense.id), "amount": "100"}],
            "fees": [fee],
        }
        remember("revision_original", "expense", operation, body, "/expenses")
        reason = revisions["REASON"]
        with engine.begin() as connection:
            revisions["_revise"](connection, fixture, operation)
        replacement = body | {
            "kind": "expense",
            "amount": "120",
            "description": "Replacement fictional entry",
            "splits": [{"category_id": str(expense.id), "amount": "120"}],
        }
        remember(
            "correct",
            "correct",
            operation,
            {"expected_version": 1, "reason": reason, "replacement": replacement},
            f"/operations/{operation}/corrections",
            status=200,
        )
        with engine.begin() as connection:
            revisions["_revise"](connection, fixture, operation, cancel=True)
        remember(
            "cancel",
            "cancel",
            operation,
            {"expected_version": 2, "reason": reason},
            f"/operations/{operation}/cancellations",
            status=200,
        )
        before_upgrade = financial_snapshot(engine)
    finally:
        # Restore the latest schema even on a failed assertion, before the normal fixture cleanup.
        command.upgrade(config, "head")

    assert financial_snapshot(engine) == before_upgrade
    with engine.connect() as connection:
        assert "hash_version" in {
            column["name"] for column in inspect(connection).get_columns("command_receipts")
        }
        upgraded = Table("command_receipts", MetaData(), autoload_with=connection)
        assert set(connection.scalars(select(upgraded.c.hash_version))) == {1}
    return {
        "engine": engine,
        "owner": owner,
        "ledger": ledger,
        "entity": entity,
        "account": accounts[0],
        "expense": expense,
        "posting": PostingService(engine),
        "cases": cases,
        "before_upgrade": before_upgrade,
        "snapshot": financial_snapshot,
    }
