"""Database transfer rules preserve both quantities and immutable posting history."""

import os
import runpy
from datetime import date
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from coinpup_api.ledger.models import FinancialOperation, Journal, JournalLine, OpeningPosition
from coinpup_api.ledger.schemas import AccountCreate, CategoryCreate, EntityCreate
from coinpup_api.ledger.service import LedgerService
from sqlalchemy import func, insert, select
from sqlalchemy.exc import IntegrityError

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("COINPUP_RUN_DB_TESTS") != "1",
        reason="Requires an explicitly configured disposable PostgreSQL database",
    ),
]
DAY = date(2026, 1, 1)


@pytest.fixture
def transfer_structure(structure_database):
    engine, owner = structure_database
    service = LedgerService(engine)
    first = service.create_entity(
        owner, EntityCreate(kind="personal", name="Fictional transfer person", base_asset_id="USD")
    )
    second = service.create_entity(
        owner, EntityCreate(kind="personal", name="Fictional other person", base_asset_id="USD")
    )
    accounts = [
        service.create_account(
            owner,
            ledger,
            AccountCreate(
                name="Fictional transfer account", kind=kind, asset_ids=["USD", "EUR", "ETH"]
            ),
        )
        for ledger, kind in (
            (first.ledger.id, "bank"),
            (first.ledger.id, "credit_card"),
            (second.ledger.id, "bank"),
        )
    ]
    category = service.create_category(
        owner, first.ledger.id, CategoryCreate(name="Fictional expense", kind="expense")
    )
    return {
        "engine": engine,
        "owner": owner,
        "ledger": first.ledger.id,
        "accounts": [account.id for account in accounts],
        "category": category.id,
    }


def _header(connection, fixture, recognition_date=DAY):
    operation, journal = uuid4(), uuid4()
    connection.execute(
        insert(FinancialOperation).values(
            id=operation,
            ledger_id=fixture["ledger"],
            kind="transfer",
            current_journal_id=journal,
            created_by=fixture["owner"],
        )
    )
    connection.execute(
        insert(Journal).values(
            id=journal,
            operation_id=operation,
            ledger_id=fixture["ledger"],
            transaction_date=DAY,
            recognition_date=recognition_date,
        )
    )
    return operation, journal


def _lines(fixture, journal, asset="USD", amount="10.00"):
    quantity = Decimal(amount)
    return [
        {
            "id": uuid4(),
            "journal_id": journal,
            "ledger_id": fixture["ledger"],
            "line_no": number,
            "role": "account",
            "asset_id": asset,
            "amount": quantity.copy_negate() if number == 1 else quantity,
            "account_id": fixture["accounts"][number - 1],
            "category_id": None,
        }
        for number in (1, 2)
    ]


@pytest.mark.parametrize("asset,amount", [("USD", "10.00"), ("ETH", "0.000000000000000001")])
def test_transfer_commits_two_exact_account_lines_without_expense(
    transfer_structure, asset, amount
):
    fixture = transfer_structure
    with fixture["engine"].begin() as connection:
        _, journal = _header(connection, fixture)
        connection.execute(insert(JournalLine), _lines(fixture, journal, asset, amount))
    with fixture["engine"].connect() as connection:
        assert connection.scalar(select(Journal.sealed).where(Journal.id == journal)) is True
        rows = connection.execute(
            select(JournalLine.role, JournalLine.amount)
            .where(JournalLine.journal_id == journal)
            .order_by(JournalLine.line_no)
        ).all()
        assert rows == [("account", Decimal(amount).copy_negate()), ("account", Decimal(amount))]
        assert connection.scalar(select(func.count()).select_from(OpeningPosition)) == 0


@pytest.mark.parametrize(
    "fault",
    ["empty", "one_sided", "same_account", "mixed_asset", "unbalanced", "equity", "extra_expense"],
)
def test_invalid_transfer_shape_rolls_back_both_sides(transfer_structure, fault):
    fixture = transfer_structure
    with pytest.raises(IntegrityError) as rejected:
        with fixture["engine"].begin() as connection:
            _, journal = _header(connection, fixture)
            rows = _lines(fixture, journal)
            if fault == "one_sided":
                rows = rows[:1]
            elif fault == "same_account":
                rows[1]["account_id"] = rows[0]["account_id"]
            elif fault == "mixed_asset":
                rows[1]["asset_id"] = "EUR"
            elif fault == "unbalanced":
                rows[1]["amount"] = Decimal("9.00")
            elif fault == "equity":
                rows[1]["role"], rows[1]["account_id"] = "equity", None
            elif fault == "extra_expense":
                rows[0]["amount"] = Decimal("-15.00")
                rows.append(
                    {
                        "id": uuid4(),
                        "journal_id": journal,
                        "ledger_id": fixture["ledger"],
                        "line_no": 3,
                        "role": "expense",
                        "asset_id": "USD",
                        "amount": Decimal("5.00"),
                        "account_id": None,
                        "category_id": fixture["category"],
                    }
                )
            if fault != "empty":
                connection.execute(insert(JournalLine), rows)
    assert rejected.value.orig.diag.constraint_name in {"ck_journal_shape", "ck_journal_balanced"}
    with fixture["engine"].connect() as connection:
        assert connection.scalar(select(func.count()).select_from(FinancialOperation)) == 0
        assert connection.scalar(select(func.count()).select_from(JournalLine)) == 0


def test_transfer_destination_cannot_belong_to_another_ledger(transfer_structure):
    fixture = transfer_structure
    with pytest.raises(IntegrityError) as rejected:
        with fixture["engine"].begin() as connection:
            _, journal = _header(connection, fixture)
            rows = _lines(fixture, journal)
            rows[1]["account_id"] = fixture["accounts"][2]
            connection.execute(insert(JournalLine), rows)
    assert rejected.value.orig.diag.constraint_name == "fk_journal_lines_account_asset_ledger"


def test_transfer_does_not_create_an_opening_position(transfer_structure):
    fixture = transfer_structure
    with pytest.raises(IntegrityError) as rejected:
        with fixture["engine"].begin() as connection:
            operation, journal = _header(connection, fixture)
            connection.execute(insert(JournalLine), _lines(fixture, journal))
            connection.execute(
                insert(OpeningPosition).values(
                    operation_id=operation,
                    ledger_id=fixture["ledger"],
                    account_id=fixture["accounts"][0],
                    asset_id="USD",
                )
            )
    assert rejected.value.orig.diag.constraint_name == "ck_opening_position"


def test_transfer_uses_the_transaction_date_for_recognition(transfer_structure):
    fixture = transfer_structure
    with pytest.raises(IntegrityError) as rejected:
        with fixture["engine"].begin() as connection:
            _, journal = _header(connection, fixture, recognition_date=date(2026, 1, 2))
            connection.execute(insert(JournalLine), _lines(fixture, journal))
    assert rejected.value.orig.diag.constraint_name == "ck_journal_shape"


def test_transfer_cannot_receive_a_later_balanced_pair(transfer_structure):
    fixture = transfer_structure
    with fixture["engine"].begin() as connection:
        _, journal = _header(connection, fixture)
        connection.execute(insert(JournalLine), _lines(fixture, journal))
    with pytest.raises(IntegrityError) as rejected:
        with fixture["engine"].begin() as connection:
            rows = _lines(fixture, journal)
            rows[0]["line_no"], rows[1]["line_no"] = 3, 4
            connection.execute(insert(JournalLine), rows)
    assert rejected.value.orig.diag.constraint_name == "ck_journal_sealed"


def test_downgrade_refuses_existing_transfer_history(transfer_structure):
    fixture = transfer_structure
    with fixture["engine"].begin() as connection:
        operation, journal = _header(connection, fixture)
        connection.execute(insert(JournalLine), _lines(fixture, journal))
    migration_path = (
        Path(__file__).resolve().parents[2]
        / "services/api/migrations/versions"
        / "20261003_0005_same_asset_transfers.py"
    )
    downgrade = runpy.run_path(str(migration_path))["downgrade"]
    with pytest.raises(IntegrityError) as rejected:
        with fixture["engine"].begin() as connection:
            with Operations.context(MigrationContext.configure(connection)):
                downgrade()
    assert rejected.value.orig.sqlstate == "23514"
    assert rejected.value.orig.diag.constraint_name == "ck_transfer_downgrade"
    with fixture["engine"].connect() as connection:
        assert connection.scalar(select(FinancialOperation.id)) == operation
        assert connection.scalar(select(Journal.sealed).where(Journal.id == journal)) is True
        assert connection.scalar(select(func.count()).select_from(JournalLine)) == 2
