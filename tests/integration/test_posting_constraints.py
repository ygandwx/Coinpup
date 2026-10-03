"""Commit-time posting invariants hold independently of the application service."""

import os
from datetime import date
from decimal import Decimal
from uuid import uuid4

import pytest
from coinpup_api.ledger.models import (
    Account,
    AccountAsset,
    Category,
    CommandReceipt,
    Entity,
    FinancialOperation,
    Journal,
    JournalLine,
    Ledger,
    OpeningPosition,
)
from sqlalchemy import delete, func, insert, select, text, update
from sqlalchemy.exc import DataError, IntegrityError

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("COINPUP_RUN_DB_TESTS") != "1",
        reason="Requires an explicitly configured disposable PostgreSQL database",
    ),
]
DAY = date(2026, 1, 1)


@pytest.fixture
def posting_structure(structure_database):
    engine, owner = structure_database
    ledgers, accounts, expense_categories, income_categories = [], [], [], []
    with engine.begin() as connection:
        for _ in range(2):
            entity, ledger, account, expense, income = [uuid4() for _ in range(5)]
            connection.execute(
                insert(Entity).values(
                    id=entity, owner_id=owner, kind="personal", name="Fictional posting person"
                )
            )
            connection.execute(
                insert(Ledger).values(
                    id=ledger, entity_id=entity, owner_id=owner, base_asset_id="USD"
                )
            )
            connection.execute(
                insert(Account).values(
                    id=account, ledger_id=ledger, name="Fictional posting wallet", kind="wise"
                )
            )
            for asset in ("USD", "EUR", "ETH"):
                connection.execute(
                    insert(AccountAsset).values(
                        account_id=account, ledger_id=ledger, asset_id=asset
                    )
                )
            for identifier, kind in ((expense, "expense"), (income, "income")):
                connection.execute(
                    insert(Category).values(
                        id=identifier, ledger_id=ledger, kind=kind, name="Fictional category"
                    )
                )
            ledgers.append(ledger)
            accounts.append(account)
            expense_categories.append(expense)
            income_categories.append(income)
    return {
        "engine": engine,
        "owner": owner,
        "ledgers": ledgers,
        "accounts": accounts,
        "expenses": expense_categories,
        "incomes": income_categories,
    }


def _header(connection, fixture, kind="expense", *, pointer=None, version=1):
    operation, journal = uuid4(), uuid4()
    connection.execute(
        insert(FinancialOperation).values(
            id=operation,
            ledger_id=fixture["ledgers"][0],
            kind=kind,
            current_journal_id=pointer or journal,
            created_by=fixture["owner"],
            version=version,
        )
    )
    connection.execute(
        insert(Journal).values(
            id=journal,
            operation_id=operation,
            ledger_id=fixture["ledgers"][0],
            operation_version=version,
            transaction_date=DAY,
            recognition_date=DAY,
        )
    )
    return operation, journal


def _lines(fixture, journal, kind="expense", amount="10.00", asset="USD"):
    quantity = Decimal(amount)
    account_amount = quantity.copy_negate() if kind == "expense" else quantity
    return [
        {
            "id": uuid4(),
            "journal_id": journal,
            "ledger_id": fixture["ledgers"][0],
            "line_no": 1,
            "role": "account",
            "asset_id": asset,
            "amount": account_amount,
            "account_id": fixture["accounts"][0],
            "category_id": None,
        },
        {
            "id": uuid4(),
            "journal_id": journal,
            "ledger_id": fixture["ledgers"][0],
            "line_no": 2,
            "role": "equity" if kind == "opening" else kind,
            "asset_id": asset,
            "amount": account_amount.copy_negate(),
            "account_id": None,
            "category_id": (
                None
                if kind == "opening"
                else fixture["expenses" if kind == "expense" else "incomes"][0]
            ),
        },
    ]


def _opening_marker(connection, fixture, operation, asset="USD", account=None):
    connection.execute(
        insert(OpeningPosition).values(
            account_id=account or fixture["accounts"][0],
            asset_id=asset,
            ledger_id=fixture["ledgers"][0],
            operation_id=operation,
        )
    )


def _post(connection, fixture, kind="expense", amount="10.00", asset="USD"):
    operation, journal = _header(connection, fixture, kind)
    rows = _lines(fixture, journal, kind, amount, asset)
    connection.execute(insert(JournalLine), rows)
    if kind == "opening":
        _opening_marker(connection, fixture, operation, asset)
    return operation, journal, rows


@pytest.mark.parametrize("kind", ["opening", "income", "expense"])
def test_valid_posting_is_sealed_on_commit(posting_structure, kind):
    fixture = posting_structure
    with fixture["engine"].begin() as connection:
        _, journal, _ = _post(connection, fixture, kind)
        assert connection.scalar(select(Journal.sealed).where(Journal.id == journal)) is False
    with fixture["engine"].connect() as connection:
        assert connection.scalar(select(Journal.sealed).where(Journal.id == journal)) is True
        assert (
            connection.scalar(
                select(func.sum(JournalLine.amount)).where(JournalLine.journal_id == journal)
            )
            == 0
        )


@pytest.mark.parametrize("shape", ["empty", "one_sided", "unbalanced", "mixed_asset"])
def test_invalid_journal_rolls_back_at_commit(posting_structure, shape):
    fixture = posting_structure
    with pytest.raises(IntegrityError):
        with fixture["engine"].begin() as connection:
            _, journal = _header(connection, fixture)
            rows = _lines(fixture, journal)
            if shape == "one_sided":
                rows = rows[:1]
            elif shape == "unbalanced":
                rows[1]["amount"] = Decimal("9.00")
            elif shape == "mixed_asset":
                rows[1]["asset_id"] = "EUR"
            if shape != "empty":
                connection.execute(insert(JournalLine), rows)
            # Still pending: validation must wait for the whole transaction's line set.
            assert connection.scalar(select(func.count()).select_from(FinancialOperation)) == 1
    with fixture["engine"].connect() as connection:
        assert connection.scalar(select(func.count()).select_from(FinancialOperation)) == 0
        assert connection.scalar(select(func.count()).select_from(JournalLine)) == 0


@pytest.mark.parametrize("amount", ["0.00", "NaN", "0.001", "100000000000000000000"])
def test_invalid_stored_quantities_are_rejected(posting_structure, amount):
    with pytest.raises((IntegrityError, DataError)) as rejected:
        with posting_structure["engine"].begin() as connection:
            _post(connection, posting_structure, amount=amount)
    # Numeric overflow is DataError; finite/zero/asset precision failures are IntegrityError.
    assert getattr(rejected.value.orig, "sqlstate", None) in {"23514", "22003"}


def test_full_eth_precision_is_stored_exactly(posting_structure):
    fixture = posting_structure
    original = "99999999999999999999.999999999999999999"
    with fixture["engine"].begin() as connection:
        _, journal, _ = _post(connection, fixture, amount=original, asset="ETH")
    with fixture["engine"].connect() as connection:
        stored = connection.scalar(
            select(JournalLine.amount).where(
                JournalLine.journal_id == journal, JournalLine.role == "expense"
            )
        )
    assert stored == Decimal(original)


@pytest.mark.parametrize("reference", ["account", "category", "journal"])
def test_journal_lines_cannot_cross_ledgers(posting_structure, reference):
    fixture = posting_structure
    with pytest.raises(IntegrityError) as rejected:
        with fixture["engine"].begin() as connection:
            _, journal = _header(connection, fixture)
            rows = _lines(fixture, journal)
            if reference == "account":
                rows[0]["account_id"] = fixture["accounts"][1]
            elif reference == "category":
                rows[1]["category_id"] = fixture["expenses"][1]
            else:
                rows[0]["ledger_id"] = fixture["ledgers"][1]
            connection.execute(insert(JournalLine), rows)
    assert rejected.value.orig.sqlstate == "23503"


def test_category_kind_is_bound_to_line_role(posting_structure):
    fixture = posting_structure
    with pytest.raises(IntegrityError) as rejected:
        with fixture["engine"].begin() as connection:
            _, journal = _header(connection, fixture)
            rows = _lines(fixture, journal)
            rows[1]["category_id"] = fixture["incomes"][0]
            connection.execute(insert(JournalLine), rows)
    assert rejected.value.orig.diag.constraint_name == "fk_journal_lines_category_ledger_kind"


@pytest.mark.parametrize("kind", ["income", "expense"])
def test_negative_income_or_expense_cannot_reverse_normal_signs(posting_structure, kind):
    with pytest.raises(IntegrityError) as rejected:
        with posting_structure["engine"].begin() as connection:
            _post(connection, posting_structure, kind, amount="-10.00")
    assert rejected.value.orig.diag.constraint_name == "ck_journal_shape"


def test_balanced_late_append_cannot_modify_a_sealed_journal(posting_structure):
    fixture = posting_structure
    with fixture["engine"].begin() as connection:
        _, journal, _ = _post(connection, fixture)
    rows = _lines(fixture, journal)
    for row in rows:
        row["line_no"] += 2
    with pytest.raises(IntegrityError) as rejected:
        with fixture["engine"].begin() as connection:
            connection.execute(insert(JournalLine), rows)
    assert rejected.value.orig.diag.constraint_name == "ck_journal_sealed"
    with fixture["engine"].connect() as connection:
        assert connection.scalar(select(func.count()).select_from(JournalLine)) == 2


@pytest.mark.parametrize("target", ["operation", "header", "unseal", "line_update", "line_delete"])
def test_committed_financial_records_are_immutable(posting_structure, target):
    fixture = posting_structure
    with fixture["engine"].begin() as connection:
        operation, journal, rows = _post(connection, fixture)
    statements = {
        "operation": update(FinancialOperation)
        .where(FinancialOperation.id == operation)
        .values(version=2),
        "header": update(Journal).where(Journal.id == journal).values(description="Changed"),
        "unseal": update(Journal).where(Journal.id == journal).values(sealed=False),
        "line_update": update(JournalLine)
        .where(JournalLine.id == rows[0]["id"])
        .values(amount=Decimal("-20")),
        "line_delete": delete(JournalLine).where(JournalLine.id == rows[0]["id"]),
    }
    with pytest.raises(IntegrityError) as rejected:
        with fixture["engine"].begin() as connection:
            connection.execute(statements[target])
    assert rejected.value.orig.diag.constraint_name == "ck_posting_immutable"


@pytest.mark.parametrize("fault", ["missing_journal", "wrong_current", "later_revision"])
def test_initial_operation_requires_its_own_first_current_journal(posting_structure, fault):
    fixture = posting_structure
    with pytest.raises(IntegrityError):
        with fixture["engine"].begin() as connection:
            if fault == "missing_journal":
                connection.execute(
                    insert(FinancialOperation).values(
                        id=uuid4(),
                        ledger_id=fixture["ledgers"][0],
                        kind="expense",
                        current_journal_id=uuid4(),
                        created_by=fixture["owner"],
                    )
                )
            else:
                _, journal = _header(
                    connection,
                    fixture,
                    pointer=uuid4() if fault == "wrong_current" else None,
                    version=2 if fault == "later_revision" else 1,
                )
                connection.execute(insert(JournalLine), _lines(fixture, journal))


def test_current_pointer_cannot_claim_another_operations_journal(posting_structure):
    fixture = posting_structure
    with fixture["engine"].begin() as connection:
        _, existing_journal, _ = _post(connection, fixture)
    with pytest.raises(IntegrityError) as rejected:
        with fixture["engine"].begin() as connection:
            _, journal = _header(connection, fixture, pointer=existing_journal)
            connection.execute(insert(JournalLine), _lines(fixture, journal))
    assert rejected.value.orig.diag.constraint_name in {
        "fk_financial_operations_current_journal",
        "ck_journal_shape",
    }


@pytest.mark.parametrize("fault", ["missing_marker", "wrong_asset", "not_opening"])
def test_opening_marker_must_match_the_actual_opening(posting_structure, fault):
    fixture = posting_structure
    with pytest.raises(IntegrityError) as rejected:
        with fixture["engine"].begin() as connection:
            kind = "expense" if fault == "not_opening" else "opening"
            operation, journal = _header(connection, fixture, kind)
            connection.execute(insert(JournalLine), _lines(fixture, journal, kind))
            if fault != "missing_marker":
                _opening_marker(
                    connection, fixture, operation, asset="EUR" if fault == "wrong_asset" else "USD"
                )
    assert rejected.value.orig.diag.constraint_name == "ck_opening_position"


def test_one_opening_per_account_asset_and_negative_opening_allowed(posting_structure):
    fixture = posting_structure
    with fixture["engine"].begin() as connection:
        _post(connection, fixture, "opening", amount="-10.00")
    with pytest.raises(IntegrityError) as rejected:
        with fixture["engine"].begin() as connection:
            _post(connection, fixture, "opening", amount="20.00")
    assert rejected.value.orig.sqlstate == "23505"
    with fixture["engine"].connect() as connection:
        assert connection.scalar(select(func.count()).select_from(FinancialOperation)) == 1


def test_receipt_is_immutable_and_commits_with_the_posting(posting_structure):
    fixture = posting_structure
    with fixture["engine"].begin() as connection:
        operation, _, _ = _post(connection, fixture)
        connection.execute(
            insert(CommandReceipt).values(
                ledger_id=fixture["ledgers"][0],
                key="fictional-command",
                request_hash="a" * 64,
                response={"id": str(operation), "amount": "10.00"},
                response_status=201,
                operation_id=operation,
            )
        )
    with pytest.raises(IntegrityError) as rejected:
        with fixture["engine"].begin() as connection:
            connection.execute(update(CommandReceipt).values(response={"amount": "20.00"}))
    assert rejected.value.orig.diag.constraint_name == "ck_posting_immutable"
    with fixture["engine"].connect() as connection:
        assert connection.scalar(select(CommandReceipt.response))["amount"] == "10.00"


def test_explicit_constraint_check_seals_and_prevents_more_lines(posting_structure):
    fixture = posting_structure
    with fixture["engine"].begin() as connection:
        _, journal, _ = _post(connection, fixture)
        connection.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
        assert connection.scalar(select(Journal.sealed).where(Journal.id == journal)) is True
        with pytest.raises(IntegrityError) as rejected:
            with connection.begin_nested():
                rows = _lines(fixture, journal)
                rows[0]["line_no"], rows[1]["line_no"] = 3, 4
                connection.execute(insert(JournalLine), rows)
        assert rejected.value.orig.diag.constraint_name == "ck_journal_sealed"
