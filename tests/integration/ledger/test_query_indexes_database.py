"""Real PostgreSQL index definitions, plans and data-preserving migration round trips."""

import json
import runpy
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import UUID

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from coinpup_api.ledger.models import (
    CommandReceipt,
    FinancialOperation,
    Journal,
    JournalLine,
    OpeningPosition,
)
from coinpup_api.ledger.posting import PostingService
from coinpup_api.ledger.posting_schemas import ExpenseCreate
from coinpup_api.ledger.schemas import AccountCreate, CategoryCreate, EntityCreate
from coinpup_api.ledger.service import LedgerService
from sqlalchemy import func, inspect, select, text

pytestmark = pytest.mark.integration
ROOT = Path(__file__).resolve().parents[3]
BALANCE_INDEX = "ix_journal_lines_account_balance"
DATE_INDEXES = {
    "transaction_date": "ix_journals_ledger_transaction_date",
    "recognition_date": "ix_journals_ledger_recognition_date",
}
INDEX_NAMES = {BALANCE_INDEX, *DATE_INDEXES.values()}


def fictional_id(number):
    return UUID(f"00000000-0011-4000-8000-{number:012d}")


@pytest.fixture
def indexed_history(structure_database):
    engine, owner = structure_database
    structure, posting = LedgerService(engine), PostingService(engine)
    entity = structure.create_entity(
        owner,
        EntityCreate(
            id=fictional_id(1),
            ledger_id=fictional_id(2),
            kind="personal",
            name="Fictional index evidence",
            base_asset_id="USD",
        ),
    )
    ledger = entity.ledger.id
    category = structure.create_category(
        owner,
        ledger,
        CategoryCreate(id=fictional_id(3), name="Fictional index expense", kind="expense"),
    )
    accounts = []
    for account_number in range(16):
        account = structure.create_account(
            owner,
            ledger,
            AccountCreate(
                id=fictional_id(100 + account_number),
                name=f"Fictional wallet {account_number}",
                kind="bank",
                asset_ids=["USD"],
            ),
        )
        accounts.append(account.id)
        for day_number in range(4):
            posting.post_expense(
                owner,
                ledger,
                ExpenseCreate(
                    id=fictional_id(1000 + account_number * 4 + day_number),
                    account_id=account.id,
                    asset_id="USD",
                    amount="1.01",
                    transaction_date=date(2026, 1, 1) + timedelta(days=day_number),
                    recognition_date=date(2025, 12, 1) + timedelta(days=day_number),
                    splits=[{"category_id": category.id, "amount": "1.01"}],
                ),
                f"fictional-index-{account_number}-{day_number}",
            )
    with engine.begin() as connection:
        connection.execute(text("ANALYZE journal_lines"))
        connection.execute(text("ANALYZE journals"))
    return engine, owner, ledger, accounts, posting


def query_indexes(connection):
    inspector = inspect(connection)
    return {
        item["name"]: item
        for table in ("journal_lines", "journals")
        for item in inspector.get_indexes(table)
        if item["name"] in INDEX_NAMES
    }


def test_migrated_index_definitions_match_columns_predicate_and_include(structure_database):
    engine, _ = structure_database
    with engine.connect() as connection:
        indexes = query_indexes(connection)
        assert set(indexes) == INDEX_NAMES
        balance = indexes[BALANCE_INDEX]
        assert balance["column_names"] == ["ledger_id", "account_id", "asset_id"]
        assert balance["dialect_options"]["postgresql_include"] == ["amount"]
        predicate = balance["dialect_options"]["postgresql_where"]
        assert (
            predicate.replace("::text", "").replace("(", "").replace(")", "") == "role = 'account'"
        )
        for column, name in DATE_INDEXES.items():
            assert indexes[name]["column_names"] == ["ledger_id", column]
            assert not indexes[name]["dialect_options"].get("postgresql_include")
            assert not indexes[name]["dialect_options"].get("postgresql_where")
        assert all(not index["unique"] for index in indexes.values())


def plan_nodes(plan):
    yield plan
    for child in plan.get("Plans", []):
        yield from plan_nodes(child)


def explain(connection, statement, *, name, seqscan, capsys):
    sql = str(statement.compile(dialect=connection.dialect, compile_kwargs={"literal_binds": True}))
    document = connection.exec_driver_sql("EXPLAIN (ANALYZE, FORMAT JSON) " + sql).scalar_one()[0]
    nodes = list(plan_nodes(document["Plan"]))
    evidence = {
        "query": name,
        "enable_seqscan": seqscan,
        "fixture": {"accounts": 16, "journals": 64, "journal_lines": 128},
        "scans": [
            {
                "type": node["Node Type"],
                "index": node.get("Index Name"),
                "rows": node["Actual Rows"],
            }
            for node in nodes
            if "Scan" in node["Node Type"]
        ],
    }
    # Visible in successful CI output; these small-fixture plans are not production benchmarks.
    with capsys.disabled():
        print("OPT11_EXPLAIN " + json.dumps(evidence, sort_keys=True))
    return nodes


def test_balance_and_date_queries_can_use_the_new_indexes(indexed_history, capsys):
    engine, _, ledger, accounts, _ = indexed_history
    balance = select(func.sum(JournalLine.amount)).where(
        JournalLine.ledger_id == ledger,
        JournalLine.account_id == accounts[0],
        JournalLine.asset_id == "USD",
        JournalLine.role == "account",
    )
    queries = [("account_balance", balance, BALANCE_INDEX)]
    for column, name in DATE_INDEXES.items():
        day = date(2026, 1, 2) if column == "transaction_date" else date(2025, 12, 2)
        field = getattr(Journal, column)
        queries.append(
            (
                column,
                select(Journal.id).where(Journal.ledger_id == ledger, field >= day, field <= day),
                name,
            )
        )
    with engine.begin() as connection:
        default = connection.scalar(text("SHOW enable_seqscan"))
        assert connection.scalar(balance) == Decimal("-4.04")
        for name, statement, _ in queries:
            explain(connection, statement, name=name, seqscan=default, capsys=capsys)
        # Only this transaction disables sequential scans to demonstrate index applicability
        # when PostgreSQL reasonably prefers a sequential scan for the tiny fixture.
        connection.execute(text("SET LOCAL enable_seqscan = off"))
        for name, statement, index in queries:
            nodes = explain(connection, statement, name=name, seqscan="off", capsys=capsys)
            assert index in {node.get("Index Name") for node in nodes}
    with engine.connect() as connection:
        assert connection.scalar(text("SHOW enable_seqscan")) == default


def financial_snapshot(engine):
    with engine.connect() as connection:
        return {
            model.__tablename__: connection.execute(
                select(model.__table__).order_by(*model.__table__.primary_key.columns)
            )
            .mappings()
            .all()
            for model in (FinancialOperation, Journal, JournalLine, OpeningPosition, CommandReceipt)
        }


def test_query_index_migration_round_trip_keeps_history_and_receipts(indexed_history):
    engine, owner, ledger, _, posting = indexed_history
    before = financial_snapshot(engine)
    balances = posting.balances(owner, ledger)
    migration = runpy.run_path(
        str(ROOT / "services/api/migrations/versions/20261004_0010_ledger_query_indexes.py")
    )
    with engine.connect() as connection:
        original_head = connection.scalar(text("SELECT version_num FROM alembic_version"))
    downgraded = False
    try:
        # Exercise only this frozen index migration; later protected history migrations must
        # never be downgraded by an index test. The standard DB check verifies the full chain.
        with engine.begin() as connection:
            with Operations.context(MigrationContext.configure(connection)):
                migration["downgrade"]()
        downgraded = True
        with engine.connect() as connection:
            assert not query_indexes(connection)
        assert financial_snapshot(engine) == before
        assert posting.balances(owner, ledger) == balances
    finally:
        if downgraded:
            with engine.begin() as connection:
                with Operations.context(MigrationContext.configure(connection)):
                    migration["upgrade"]()
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == original_head
        assert set(query_indexes(connection)) == INDEX_NAMES
    assert financial_snapshot(engine) == before
    assert posting.balances(owner, ledger) == balances
