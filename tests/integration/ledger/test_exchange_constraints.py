"""Principal exchange quantities and every fee balance independently at commit."""

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
def exchange_structure(structure_database):
    engine, owner = structure_database
    service = LedgerService(engine)
    ledgers = [
        service.create_entity(
            owner,
            EntityCreate(kind="personal", name="Fictional exchange person", base_asset_id="USD"),
        ).ledger.id
        for _ in range(2)
    ]
    accounts = [
        service.create_account(
            owner,
            ledger,
            AccountCreate(
                name="Fictional exchange wallet",
                kind="wise",
                asset_ids=["USD", "EUR", "ETH", "BTC"],
            ),
        ).id
        for ledger in (ledgers[0], ledgers[0], ledgers[1])
    ]
    categories = [
        service.create_category(
            owner, ledger, CategoryCreate(name="Fictional fee", kind="expense")
        ).id
        for ledger in ledgers
    ]
    income = service.create_category(
        owner, ledgers[0], CategoryCreate(name="Fictional income", kind="income")
    ).id
    return {
        "engine": engine,
        "owner": owner,
        "ledger": ledgers[0],
        "accounts": accounts,
        "categories": categories,
        "income": income,
    }


def _header(connection, fixture, kind="exchange", recognition_date=DAY):
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
            recognition_date=recognition_date,
        )
    )
    return operation, journal


def _line(
    fixture, journal, number, role, asset, amount, *, component=0, account=None, category=None
):
    return {
        "id": uuid4(),
        "journal_id": journal,
        "ledger_id": fixture["ledger"],
        "line_no": number,
        "component_no": component,
        "role": role,
        "asset_id": asset,
        "amount": Decimal(amount),
        "account_id": account,
        "category_id": category,
    }


def _principal(fixture, journal, same_account=False):
    return [
        _line(fixture, journal, 1, "account", "USD", "-100", account=fixture["accounts"][0]),
        _line(fixture, journal, 2, "exchange", "USD", "100"),
        _line(
            fixture,
            journal,
            3,
            "account",
            "EUR",
            "90",
            account=fixture["accounts"][0 if same_account else 1],
        ),
        _line(fixture, journal, 4, "exchange", "EUR", "-90"),
    ]


def _fee(fixture, journal, component=1, asset="USD", amount="2", start=5):
    return [
        _line(
            fixture,
            journal,
            start,
            "account",
            asset,
            Decimal(amount).copy_negate(),
            component=component,
            account=fixture["accounts"][0],
        ),
        _line(
            fixture,
            journal,
            start + 1,
            "expense",
            asset,
            amount,
            component=component,
            category=fixture["categories"][0],
        ),
    ]


def _legacy_principal(fixture, journal, kind):
    if kind == "transfer":
        return [
            _line(fixture, journal, 1, "account", "USD", "-100", account=fixture["accounts"][0]),
            _line(fixture, journal, 2, "account", "USD", "100", account=fixture["accounts"][1]),
        ]
    if kind == "opening":
        return [
            _line(fixture, journal, 1, "account", "USD", "100", account=fixture["accounts"][0]),
            _line(fixture, journal, 2, "equity", "USD", "-100"),
        ]
    if kind == "income":
        return [
            _line(fixture, journal, 1, "account", "USD", "100", account=fixture["accounts"][0]),
            _line(fixture, journal, 2, "income", "USD", "-100", category=fixture["income"]),
        ]
    return [
        _line(fixture, journal, 1, "account", "USD", "-100", account=fixture["accounts"][0]),
        _line(fixture, journal, 2, "expense", "USD", "100", category=fixture["categories"][0]),
    ]


@pytest.mark.parametrize("same_account", [False, True])
def test_exchange_principal_and_third_asset_fees_are_independent(exchange_structure, same_account):
    fixture = exchange_structure
    with fixture["engine"].begin() as connection:
        _, journal = _header(connection, fixture)
        rows = _principal(fixture, journal, same_account)
        rows += _fee(fixture, journal)
        rows += _fee(
            fixture, journal, component=2, asset="ETH", amount="0.000000000000000001", start=7
        )
        connection.execute(insert(JournalLine), rows)
    with fixture["engine"].connect() as connection:
        assert connection.scalar(select(Journal.sealed).where(Journal.id == journal)) is True
        expenses = dict(
            connection.execute(
                select(JournalLine.asset_id, func.sum(JournalLine.amount))
                .where(JournalLine.role == "expense")
                .group_by(JournalLine.asset_id)
            ).all()
        )
        assert expenses == {"USD": Decimal("2"), "ETH": Decimal("0.000000000000000001")}
        assert (
            connection.execute(
                select(JournalLine.asset_id)
                .group_by(JournalLine.asset_id)
                .having(func.sum(JournalLine.amount) != 0)
            ).all()
            == []
        )


@pytest.mark.parametrize("kind", ["transfer", "income", "expense"])
def test_legacy_principal_shape_excludes_fee_components(exchange_structure, kind):
    fixture = exchange_structure
    with fixture["engine"].begin() as connection:
        _, journal = _header(connection, fixture, kind)
        connection.execute(
            insert(JournalLine), _legacy_principal(fixture, journal, kind) + _fee(fixture, journal)
        )
    with fixture["engine"].connect() as connection:
        assert connection.scalar(select(Journal.sealed).where(Journal.id == journal)) is True


@pytest.mark.parametrize(
    "fault", ["same_asset", "missing_counter", "wrong_role", "same_direction", "unbalanced"]
)
def test_malformed_exchange_principal_is_rejected(exchange_structure, fault):
    fixture = exchange_structure
    with pytest.raises(IntegrityError) as rejected:
        with fixture["engine"].begin() as connection:
            _, journal = _header(connection, fixture)
            rows = _principal(fixture, journal)
            if fault == "same_asset":
                rows[2]["asset_id"] = rows[3]["asset_id"] = "USD"
            elif fault == "missing_counter":
                rows.pop()
            elif fault == "wrong_role":
                rows[1]["role"] = "equity"
            elif fault == "same_direction":
                rows[2]["amount"], rows[3]["amount"] = Decimal("-90"), Decimal("90")
            else:
                rows[3]["amount"] = Decimal("-80")
            connection.execute(insert(JournalLine), rows)
    assert rejected.value.orig.diag.constraint_name in {"ck_journal_shape", "ck_journal_balanced"}
    with fixture["engine"].connect() as connection:
        assert connection.scalar(select(func.count()).select_from(FinancialOperation)) == 0


@pytest.mark.parametrize(
    "fault", ["gap", "one_sided", "mixed_asset", "unbalanced", "wrong_sign", "wrong_role"]
)
def test_every_fee_component_requires_its_own_valid_pair(exchange_structure, fault):
    fixture = exchange_structure
    with pytest.raises(IntegrityError) as rejected:
        with fixture["engine"].begin() as connection:
            _, journal = _header(connection, fixture)
            fees = _fee(fixture, journal)
            if fault == "gap":
                fees[0]["component_no"] = fees[1]["component_no"] = 2
            elif fault == "one_sided":
                fees.pop()
            elif fault == "mixed_asset":
                fees[1]["asset_id"] = "EUR"
            elif fault == "unbalanced":
                fees[1]["amount"] = Decimal("1")
            elif fault == "wrong_sign":
                fees[0]["amount"], fees[1]["amount"] = Decimal("2"), Decimal("-2")
            else:
                fees[1]["role"], fees[1]["category_id"] = "exchange", None
            connection.execute(insert(JournalLine), _principal(fixture, journal) + fees)
    assert rejected.value.orig.diag.constraint_name == "ck_journal_fees"


@pytest.mark.parametrize("component", [-1, 21])
def test_component_numbers_are_bounded(exchange_structure, component):
    fixture = exchange_structure
    with pytest.raises(IntegrityError) as rejected:
        with fixture["engine"].begin() as connection:
            _, journal = _header(connection, fixture)
            connection.execute(
                insert(JournalLine),
                _principal(fixture, journal) + _fee(fixture, journal, component=component),
            )
    assert rejected.value.orig.diag.constraint_name == "ck_journal_lines_component"


@pytest.mark.parametrize("reference", ["principal_account", "fee_account", "fee_category"])
def test_exchange_and_fee_references_remain_in_the_ledger(exchange_structure, reference):
    fixture = exchange_structure
    with pytest.raises(IntegrityError) as rejected:
        with fixture["engine"].begin() as connection:
            _, journal = _header(connection, fixture)
            rows, fees = _principal(fixture, journal), _fee(fixture, journal)
            if reference == "principal_account":
                rows[2]["account_id"] = fixture["accounts"][2]
            elif reference == "fee_account":
                fees[0]["account_id"] = fixture["accounts"][2]
            else:
                fees[1]["category_id"] = fixture["categories"][1]
            connection.execute(insert(JournalLine), rows + fees)
    assert rejected.value.orig.sqlstate == "23503"


def test_opening_cannot_have_fees(exchange_structure):
    fixture = exchange_structure
    with pytest.raises(IntegrityError) as rejected:
        with fixture["engine"].begin() as connection:
            operation, journal = _header(connection, fixture, "opening")
            connection.execute(
                insert(JournalLine),
                _legacy_principal(fixture, journal, "opening") + _fee(fixture, journal),
            )
            connection.execute(
                insert(OpeningPosition).values(
                    operation_id=operation,
                    ledger_id=fixture["ledger"],
                    account_id=fixture["accounts"][0],
                    asset_id="USD",
                )
            )
    assert rejected.value.orig.diag.constraint_name == "ck_journal_fees"


def test_principal_cannot_use_an_unbalanced_fee_to_hide_a_difference(exchange_structure):
    fixture = exchange_structure
    with pytest.raises(IntegrityError) as rejected:
        with fixture["engine"].begin() as connection:
            _, journal = _header(connection, fixture, "transfer")
            rows = _legacy_principal(fixture, journal, "transfer")
            rows[1]["amount"] = Decimal("99")
            fees = _fee(fixture, journal)
            fees[1]["amount"] = Decimal("3")
            # Combined USD sum is zero, but principal (-1) and fee (+1) are invalid separately.
            connection.execute(insert(JournalLine), rows + fees)
    assert rejected.value.orig.diag.constraint_name == "ck_journal_fees"


def test_sealed_exchange_cannot_receive_late_fees(exchange_structure):
    fixture = exchange_structure
    with fixture["engine"].begin() as connection:
        _, journal = _header(connection, fixture)
        connection.execute(insert(JournalLine), _principal(fixture, journal))
    with pytest.raises(IntegrityError) as rejected:
        with fixture["engine"].begin() as connection:
            connection.execute(insert(JournalLine), _fee(fixture, journal))
    assert rejected.value.orig.diag.constraint_name == "ck_journal_sealed"


@pytest.mark.parametrize("history", ["exchange", "transfer_fee"])
def test_downgrade_refuses_exchange_or_fee_history(exchange_structure, history):
    fixture = exchange_structure
    with fixture["engine"].begin() as connection:
        kind = "exchange" if history == "exchange" else "transfer"
        operation, journal = _header(connection, fixture, kind)
        rows = (
            _principal(fixture, journal)
            if kind == "exchange"
            else (_legacy_principal(fixture, journal, kind) + _fee(fixture, journal))
        )
        connection.execute(insert(JournalLine), rows)
    migration_path = (
        Path(__file__).resolve().parents[3]
        / "services/api/migrations/versions"
        / "20261003_0006_exchange_fees.py"
    )
    downgrade = runpy.run_path(str(migration_path))["downgrade"]
    with pytest.raises(IntegrityError) as rejected:
        with fixture["engine"].begin() as connection:
            with Operations.context(MigrationContext.configure(connection)):
                downgrade()
    assert rejected.value.orig.sqlstate == "23514"
    assert rejected.value.orig.diag.constraint_name == "ck_exchange_downgrade"
    with fixture["engine"].connect() as connection:
        assert connection.scalar(select(FinancialOperation.id)) == operation
        assert connection.scalar(select(Journal.sealed).where(Journal.id == journal)) is True
