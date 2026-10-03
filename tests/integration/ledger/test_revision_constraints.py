"""Financial revisions append exact inverses and retain a complete immutable chain."""

import os
import runpy
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from coinpup_api.ledger.models import (
    Account,
    AccountAsset,
    AssetRecord,
    Category,
    FinancialOperation,
    Journal,
    JournalLine,
    OpeningPosition,
)
from coinpup_api.ledger.schemas import AccountCreate, CategoryCreate, EntityCreate
from coinpup_api.ledger.service import LedgerService
from sqlalchemy import func, insert, select, update
from sqlalchemy.exc import DataError, IntegrityError

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("COINPUP_RUN_DB_TESTS") != "1",
        reason="Requires an explicitly configured disposable PostgreSQL database",
    ),
]
DAY = date(2026, 1, 1)
REASON = "Correct fictional entry"


@pytest.fixture
def revision_structure(structure_database):
    engine, owner = structure_database
    service = LedgerService(engine)
    ledger = service.create_entity(
        owner, EntityCreate(kind="personal", name="Fictional revision person", base_asset_id="USD")
    ).ledger.id
    accounts = [
        service.create_account(
            owner,
            ledger,
            AccountCreate(name="Fictional wallet", kind="cash", asset_ids=["USD", "ETH"]),
        ).id
        for _ in range(2)
    ]
    categories = [
        service.create_category(
            owner, ledger, CategoryCreate(name="Fictional expense", kind="expense")
        ).id
        for _ in range(2)
    ]
    return {
        "engine": engine,
        "owner": owner,
        "ledger": ledger,
        "accounts": accounts,
        "categories": categories,
    }


def _initial(connection, fixture, kind="expense", amount="100", *, fees=False, asset="USD"):
    operation, journal = uuid4(), uuid4()
    connection.execute(
        insert(FinancialOperation).values(
            id=operation,
            ledger_id=fixture["ledger"],
            kind=kind,
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
            recognition_date=DAY,
            description="Original fictional entry",
        )
    )
    quantity = Decimal(amount)
    account_quantity = quantity if kind == "opening" else quantity.copy_negate()
    rows = [
        {
            "id": uuid4(),
            "journal_id": journal,
            "ledger_id": fixture["ledger"],
            "line_no": 1,
            "component_no": 0,
            "role": "account",
            "asset_id": asset,
            "amount": account_quantity,
            "account_id": fixture["accounts"][0],
            "category_id": None,
        },
        {
            "id": uuid4(),
            "journal_id": journal,
            "ledger_id": fixture["ledger"],
            "line_no": 2,
            "component_no": 0,
            "role": "equity" if kind == "opening" else "expense",
            "asset_id": asset,
            "amount": account_quantity.copy_negate(),
            "account_id": None,
            "category_id": None if kind == "opening" else fixture["categories"][0],
        },
    ]
    if fees:
        for number, role, value in ((3, "account", "-2"), (4, "expense", "2")):
            rows.append(
                {
                    "id": uuid4(),
                    "journal_id": journal,
                    "ledger_id": fixture["ledger"],
                    "line_no": number,
                    "component_no": 1,
                    "role": role,
                    "asset_id": asset,
                    "amount": Decimal(value),
                    "account_id": fixture["accounts"][0] if role == "account" else None,
                    "category_id": fixture["categories"][0] if role == "expense" else None,
                }
            )
    connection.execute(insert(JournalLine), rows)
    if kind == "opening":
        connection.execute(
            insert(OpeningPosition).values(
                operation_id=operation,
                ledger_id=fixture["ledger"],
                account_id=fixture["accounts"][0],
                asset_id=asset,
            )
        )
    return operation, journal


def _revise(
    connection, fixture, operation_id, *, cancel=False, amount="120", fault=None, reason=REASON
):
    operation = (
        connection.execute(
            select(FinancialOperation.__table__).where(FinancialOperation.id == operation_id)
        )
        .mappings()
        .one()
    )
    source = (
        connection.execute(
            select(Journal.__table__).where(Journal.id == operation["current_journal_id"])
        )
        .mappings()
        .one()
    )
    source_lines = (
        connection.execute(
            select(JournalLine.__table__)
            .where(JournalLine.journal_id == source["id"])
            .order_by(JournalLine.line_no)
        )
        .mappings()
        .all()
    )
    revision = operation["version"] + 1
    reversal, replacement = uuid4(), None if cancel else uuid4()
    if fault != "no_version":
        connection.execute(
            update(FinancialOperation)
            .where(FinancialOperation.id == operation_id)
            .values(
                version=revision,
                status="cancelled" if cancel else "active",
                current_journal_id=source["id"] if cancel else replacement,
            )
        )
    reverse_header = {
        "id": reversal,
        "operation_id": operation_id,
        "ledger_id": fixture["ledger"],
        "operation_version": revision,
        "journal_kind": "reversal",
        "reverses_journal_id": source["id"],
        "transaction_date": source["transaction_date"],
        "recognition_date": source["recognition_date"],
        "description": source["description"],
        "revision_reason": reason,
    }
    if fault == "date":
        reverse_header["transaction_date"] += timedelta(days=1)
    if fault == "description":
        reverse_header["description"] = "Changed reversal text"
    reversed_lines = [
        dict(row)
        | {
            "id": uuid4(),
            "journal_id": reversal,
            "amount": row["amount"].copy_negate(),
        }
        for row in source_lines
    ]
    if fault == "amount":
        for row in reversed_lines:
            if row["component_no"] == 0:
                row["amount"] = Decimal("101") if row["role"] == "account" else Decimal("-101")
    if fault == "account":
        reversed_lines[0]["account_id"] = fixture["accounts"][1]
    if fault == "category":
        reversed_lines[1]["category_id"] = fixture["categories"][1]
    if fault == "line_no":
        reversed_lines[0]["line_no"] = 99
    if fault == "component":
        reversed_lines[0]["component_no"] = 9
    if fault == "missing_line":
        reversed_lines.pop()
    if fault != "missing_reversal":
        connection.execute(insert(Journal), reverse_header)
        connection.execute(insert(JournalLine), reversed_lines)
    if replacement is not None and fault != "missing_replacement":
        connection.execute(
            insert(Journal).values(
                id=replacement,
                operation_id=operation_id,
                ledger_id=fixture["ledger"],
                operation_version=revision,
                transaction_date=DAY,
                recognition_date=DAY,
                description="Replacement fictional entry",
                revision_reason="Different reason" if fault == "reason_mismatch" else reason,
            )
        )
        replacement_lines = []
        for row in source_lines:
            quantity = row["amount"]
            if row["component_no"] == 0:
                new_quantity = Decimal(amount)
                quantity = new_quantity.copy_negate() if row["amount"] < 0 else new_quantity
            replacement_lines.append(
                dict(row)
                | {
                    "id": uuid4(),
                    "journal_id": replacement,
                    "amount": quantity,
                }
            )
        if fault == "opening_move":
            replacement_lines[0]["account_id"] = fixture["accounts"][1]
        connection.execute(insert(JournalLine), replacement_lines)
    return reversal, replacement


def _account_total(connection):
    return connection.scalar(
        select(func.sum(JournalLine.amount)).where(JournalLine.role == "account")
    )


def test_corrections_and_cancel_preserve_original_rows_and_exact_net(revision_structure):
    fixture = revision_structure
    with fixture["engine"].begin() as connection:
        operation, first = _initial(connection, fixture, fees=True)
    with fixture["engine"].connect() as connection:
        original = (
            connection.execute(
                select(JournalLine.__table__)
                .where(JournalLine.journal_id == first)
                .order_by(JournalLine.line_no)
            )
            .mappings()
            .all()
        )
    for amount, expected in (("120", "-122"), ("80", "-82")):
        with fixture["engine"].begin() as connection:
            _, latest = _revise(connection, fixture, operation, amount=amount)
        with fixture["engine"].connect() as connection:
            assert _account_total(connection) == Decimal(expected)
            assert (
                connection.execute(
                    select(JournalLine.__table__)
                    .where(JournalLine.journal_id == first)
                    .order_by(JournalLine.line_no)
                )
                .mappings()
                .all()
                == original
            )
    with fixture["engine"].begin() as connection:
        _revise(connection, fixture, operation, cancel=True)
    with fixture["engine"].connect() as connection:
        current = connection.execute(select(FinancialOperation.__table__)).mappings().one()
        assert (current["version"], current["status"], current["current_journal_id"]) == (
            4,
            "cancelled",
            latest,
        )
        assert _account_total(connection) == 0
        assert connection.scalar(select(func.count()).select_from(Journal)) == 6
        assert (
            connection.scalar(
                select(func.count()).select_from(Journal).where(Journal.sealed.is_(False))
            )
            == 0
        )


@pytest.mark.parametrize(
    "fault",
    [
        "amount",
        "account",
        "category",
        "line_no",
        "component",
        "missing_line",
        "date",
        "description",
    ],
)
def test_reversal_must_copy_every_original_fact_and_invert_exactly(revision_structure, fault):
    fixture = revision_structure
    with fixture["engine"].begin() as connection:
        operation, _ = _initial(connection, fixture, fees=True)
    with pytest.raises(IntegrityError) as rejected:
        with fixture["engine"].begin() as connection:
            _revise(connection, fixture, operation, fault=fault)
    assert rejected.value.orig.diag.constraint_name == "ck_journal_reversal"
    with fixture["engine"].connect() as connection:
        assert connection.scalar(select(FinancialOperation.version)) == 1
        assert _account_total(connection) == Decimal("-102")


@pytest.mark.parametrize(
    "fault", ["missing_reversal", "missing_replacement", "no_version", "reason_mismatch"]
)
def test_incomplete_or_unversioned_correction_rolls_back(revision_structure, fault):
    fixture = revision_structure
    with fixture["engine"].begin() as connection:
        operation, _ = _initial(connection, fixture)
    with pytest.raises(IntegrityError) as rejected:
        with fixture["engine"].begin() as connection:
            _revise(connection, fixture, operation, fault=fault)
    assert rejected.value.orig.diag.constraint_name in {
        "ck_operation_history",
        "fk_financial_operations_current_journal",
    }
    with fixture["engine"].connect() as connection:
        assert connection.scalar(select(func.count()).select_from(Journal)) == 1
        assert _account_total(connection) == Decimal("-100")


@pytest.mark.parametrize("reason", [None, "", "  ", "\n", " padded ", "x" * 1001])
def test_revision_reason_is_required_trimmed_and_bounded(revision_structure, reason):
    fixture = revision_structure
    with fixture["engine"].begin() as connection:
        operation, _ = _initial(connection, fixture)
    with pytest.raises((IntegrityError, DataError)) as rejected:
        with fixture["engine"].begin() as connection:
            _revise(connection, fixture, operation, reason=reason)
    assert rejected.value.orig.sqlstate in {"23514", "22001"}


def test_cancel_can_reverse_archived_and_disabled_original_targets(revision_structure):
    fixture = revision_structure
    with fixture["engine"].begin() as connection:
        operation, _ = _initial(connection, fixture, fees=True)
        connection.execute(update(Account).values(archived=True))
        connection.execute(update(AccountAsset).values(enabled=False))
        connection.execute(update(Category).values(archived=True))
        connection.execute(
            update(AssetRecord).where(AssetRecord.asset_id == "USD").values(enabled=False)
        )
    with fixture["engine"].begin() as connection:
        _revise(connection, fixture, operation, cancel=True)
    with fixture["engine"].connect() as connection:
        assert _account_total(connection) == 0


def test_cancelled_operation_cannot_be_reactivated(revision_structure):
    fixture = revision_structure
    with fixture["engine"].begin() as connection:
        operation, _ = _initial(connection, fixture)
    with fixture["engine"].begin() as connection:
        _revise(connection, fixture, operation, cancel=True)
    with pytest.raises(IntegrityError) as rejected:
        with fixture["engine"].begin() as connection:
            connection.execute(
                update(FinancialOperation).values(
                    status="active", version=3, current_journal_id=uuid4()
                )
            )
    assert rejected.value.orig.diag.constraint_name == "ck_posting_immutable"


def test_opening_correction_preserves_its_account_asset_reservation(revision_structure):
    fixture = revision_structure
    with fixture["engine"].begin() as connection:
        operation, _ = _initial(connection, fixture, "opening")
    with pytest.raises(IntegrityError) as rejected:
        with fixture["engine"].begin() as connection:
            _revise(connection, fixture, operation, fault="opening_move")
    assert rejected.value.orig.diag.constraint_name == "ck_opening_position"
    with fixture["engine"].begin() as connection:
        _revise(connection, fixture, operation, amount="120")
    with fixture["engine"].begin() as connection:
        _revise(connection, fixture, operation, cancel=True)
    with fixture["engine"].connect() as connection:
        assert connection.scalar(select(OpeningPosition.operation_id)) == operation
        assert _account_total(connection) == 0
    with pytest.raises(IntegrityError) as duplicate:
        with fixture["engine"].begin() as connection:
            _initial(connection, fixture, "opening")
    assert duplicate.value.orig.sqlstate == "23505"


def test_full_precision_eth_reversal_is_exact(revision_structure):
    fixture = revision_structure
    with fixture["engine"].begin() as connection:
        operation, _ = _initial(
            connection, fixture, amount="99999999999999999999.999999999999999999", asset="ETH"
        )
    with fixture["engine"].begin() as connection:
        _revise(connection, fixture, operation, cancel=True)
    with fixture["engine"].connect() as connection:
        assert _account_total(connection) == 0


@pytest.mark.parametrize("cancel", [False, True])
def test_downgrade_refuses_any_revision_history(revision_structure, cancel):
    fixture = revision_structure
    with fixture["engine"].begin() as connection:
        operation, _ = _initial(connection, fixture)
    with fixture["engine"].begin() as connection:
        _revise(connection, fixture, operation, cancel=cancel)
    path = (
        Path(__file__).resolve().parents[3]
        / "services/api/migrations/versions/20261003_0007_operation_revisions.py"
    )
    downgrade = runpy.run_path(str(path))["downgrade"]
    with pytest.raises(IntegrityError) as rejected:
        with fixture["engine"].begin() as connection:
            with Operations.context(MigrationContext.configure(connection)):
                downgrade()
    assert rejected.value.orig.diag.constraint_name == "ck_revision_downgrade"
    with fixture["engine"].connect() as connection:
        assert connection.scalar(select(FinancialOperation.version)) == 2
