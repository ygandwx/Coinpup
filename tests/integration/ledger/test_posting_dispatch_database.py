"""Frozen-shape upgrades preserve history, deferred reversals and first-error semantics."""

import runpy
from datetime import date
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

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
from coinpup_api.ledger.posting_schemas import (
    CancellationCreate,
    CorrectionCreate,
    ExchangeCreate,
    ExpenseCreate,
    IncomeCreate,
    OpeningCreate,
    TransferCreate,
)
from coinpup_api.ledger.schemas import AccountCreate, CategoryCreate, EntityCreate
from coinpup_api.ledger.service import LedgerService
from sqlalchemy import func, insert, select, text
from sqlalchemy.exc import IntegrityError

pytestmark = pytest.mark.integration
ROOT = Path(__file__).resolve().parents[3]
PROTECTED_FUNCTIONS = (
    "coinpup_validate_initial_journal(uuid)",
    "coinpup_validate_operation_history(uuid)",
    "coinpup_validate_reversal(uuid)",
    "coinpup_operation_deferred_check()",
    "coinpup_journal_deferred_check()",
    "coinpup_operation_revision_guard()",
    "coinpup_journal_header_guard()",
    "coinpup_journal_line_guard()",
    "coinpup_posting_reject_change()",
)
NEW_FUNCTIONS = (
    "coinpup_validate_journal_common(uuid)",
    *(
        f"coinpup_validate_shape_{kind}(uuid,jsonb)"
        for kind in ("opening", "income", "expense", "transfer", "exchange")
    ),
)
CREATES = {
    "opening": OpeningCreate,
    "income": IncomeCreate,
    "expense": ExpenseCreate,
    "transfer": TransferCreate,
    "exchange": ExchangeCreate,
}


def function_definition(connection, signature):
    return connection.scalar(
        text("SELECT pg_get_functiondef(to_regprocedure(:signature))"),
        {"signature": "public." + signature},
    )


def protected_definitions(engine):
    with engine.connect() as connection:
        functions = {name: function_definition(connection, name) for name in PROTECTED_FUNCTIONS}
        assert all(isinstance(body, str) for body in functions.values())
        triggers = connection.execute(
            text("""
            SELECT relation.relname, trigger.tgname, pg_get_triggerdef(trigger.oid),
                   trigger.tgdeferrable, trigger.tginitdeferred
            FROM pg_trigger trigger
            JOIN pg_class relation ON relation.oid = trigger.tgrelid
            JOIN pg_namespace namespace ON namespace.oid = relation.relnamespace
            WHERE namespace.nspname = 'public' AND NOT trigger.tgisinternal
              AND relation.relname IN ('financial_operations', 'journals', 'journal_lines',
                                       'opening_positions', 'command_receipts')
            ORDER BY relation.relname, trigger.tgname
        """)
        ).all()
        assert {row.tgname for row in triggers} >= {
            "tr_financial_operations_deferred",
            "tr_journals_deferred",
            "tr_journal_lines_deferred",
        }
        return functions, triggers


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


def apply_migration(engine, migration, direction):
    # Only this function split is cycled; later history-protecting migrations stay installed.
    # The standard DB check independently exercises the real Alembic chain on an empty DB.
    with engine.begin() as connection:
        with Operations.context(MigrationContext.configure(connection)):
            migration[direction]()


@pytest.fixture
def dispatch_structure(structure_database):
    engine, owner = structure_database
    structure = LedgerService(engine)
    ledger = structure.create_entity(
        owner, EntityCreate(kind="personal", name="Fictional dispatch history", base_asset_id="USD")
    ).ledger.id
    accounts = [
        structure.create_account(
            owner,
            ledger,
            AccountCreate(
                name=f"Fictional dispatch wallet {number}",
                kind="wise",
                asset_ids=["USD", "EUR", "ETH"],
            ),
        ).id
        for number in range(2)
    ]
    categories = {
        kind: [
            structure.create_category(
                owner, ledger, CategoryCreate(name=f"Fictional {kind} {number}", kind=kind)
            ).id
            for number in range(2)
        ]
        for kind in ("income", "expense")
    }
    return {
        "engine": engine,
        "owner": owner,
        "ledger": ledger,
        "accounts": accounts,
        "categories": categories["expense"],
        "income": categories["income"],
        "posting": PostingService(engine),
        "migration": runpy.run_path(
            str(ROOT / "services/api/migrations/versions/20261004_0011_posting_shape_dispatch.py")
        ),
    }


def creation_body(s, kind, account_number):
    account, destination = s["accounts"][account_number], s["accounts"][1 - account_number]
    body = {"transaction_date": "2026-01-01", "description": "Original fictional / 原始虚构"}
    if kind == "exchange":
        body.update(
            source_account_id=account,
            source_asset_id="USD",
            source_amount="100",
            destination_account_id=destination,
            destination_asset_id="EUR",
            destination_amount="90",
        )
    else:
        body.update(asset_id="USD", amount="100")
        if kind == "transfer":
            body.update(source_account_id=account, destination_account_id=destination)
        else:
            body["account_id"] = account
        if kind in {"income", "expense"}:
            categories = s["income"] if kind == "income" else s["categories"]
            body.update(
                recognition_date="2025-12-31",
                splits=[
                    {"category_id": category, "amount": amount}
                    for category, amount in zip(categories, ("60", "40"), strict=True)
                ],
            )
    if kind != "opening":
        body["fees"] = [
            {
                "account_id": account,
                "asset_id": asset,
                "amount": amount,
                "category_id": s["categories"][0],
            }
            for asset, amount in (("USD", "2"), ("ETH", "0.000000000000000001"))
        ]
    return body


def replacement_body(s, kind, body, *, upgraded):
    replacement = body | {
        "kind": kind,
        "transaction_date": "2026-01-03" if upgraded else "2026-01-02",
        "description": "New corrected fictional" if upgraded else "Old corrected fictional",
    }
    if kind == "exchange":
        replacement.update(
            source_amount="140" if upgraded else "120",
            destination_amount="126" if upgraded else "108",
        )
    else:
        replacement["amount"] = "140" if upgraded else "120"
        if kind in {"income", "expense"}:
            categories = s["income"] if kind == "income" else s["categories"]
            replacement["splits"] = [
                {"category_id": category, "amount": amount}
                for category, amount in zip(
                    categories, ("100" if upgraded else "80", "40"), strict=True
                )
            ]
    return replacement


def assert_exact_reversals(snapshot):
    journals = {row["id"]: row for row in snapshot["journals"]}
    for reversal in journals.values():
        if reversal["journal_kind"] != "reversal":
            continue
        source = journals[reversal["reverses_journal_id"]]
        assert reversal["operation_version"] == source["operation_version"] + 1
        for field in (
            "operation_id",
            "ledger_id",
            "transaction_date",
            "recognition_date",
            "description",
        ):
            assert reversal[field] == source[field]
        original_lines = [
            row for row in snapshot["journal_lines"] if row["journal_id"] == source["id"]
        ]
        reversed_lines = [
            row for row in snapshot["journal_lines"] if row["journal_id"] == reversal["id"]
        ]
        original = sorted(
            (
                {
                    key: value.copy_negate() if key == "amount" else value
                    for key, value in row.items()
                    if key not in {"id", "journal_id"}
                }
                for row in original_lines
            ),
            key=lambda row: row["line_no"],
        )
        actual = sorted(
            (
                {key: value for key, value in row.items() if key not in {"id", "journal_id"}}
                for row in reversed_lines
            ),
            key=lambda row: row["line_no"],
        )
        assert actual == original
    assert all(row["sealed"] for row in journals.values())


def test_upgrade_with_history_keeps_wrappers_reversals_receipts_and_deferred_validation(
    dispatch_structure,
):
    s = dispatch_structure
    engine, owner, ledger, posting = s["engine"], s["owner"], s["ledger"], s["posting"]
    preserved = protected_definitions(engine)
    with engine.connect() as connection:
        original_head = connection.scalar(text("SELECT version_num FROM alembic_version"))
    old_installed = False
    commands, active = [], {}
    try:
        apply_migration(engine, s["migration"], "downgrade")
        old_installed = True
        assert protected_definitions(engine) == preserved
        with engine.connect() as connection:
            old_shape = function_definition(connection, "coinpup_validate_posting_shape(uuid)")
            assert all(function_definition(connection, name) is None for name in NEW_FUNCTIONS)
        for kind, cls in CREATES.items():
            for number in range(2):
                body = creation_body(s, kind, number)
                method = getattr(posting, "post_" + kind)
                args = (owner, ledger, cls.model_validate(body), f"before-dispatch-{kind}-{number}")
                receipt = method(*args)
                commands.append((method, args, receipt.model_dump(mode="json")))
                if number == 0:
                    request = CorrectionCreate(
                        expected_version=1,
                        reason="Old fictional correction",
                        replacement=replacement_body(s, kind, body, upgraded=False),
                    )
                    args = (owner, ledger, receipt.id, request, "before-correct-" + kind)
                    corrected = posting.correct_operation(*args)
                    commands.append(
                        (posting.correct_operation, args, corrected.model_dump(mode="json"))
                    )
                    active[kind] = (receipt.id, body)
                else:
                    args = (
                        owner,
                        ledger,
                        receipt.id,
                        CancellationCreate(expected_version=1, reason="Old fictional cancel"),
                        "before-cancel-" + kind,
                    )
                    cancelled = posting.cancel_operation(*args)
                    commands.append(
                        (posting.cancel_operation, args, cancelled.model_dump(mode="json"))
                    )
        before_upgrade = financial_snapshot(engine)
        assert len(before_upgrade["financial_operations"]) == 10
        assert len(before_upgrade["command_receipts"]) == 20
        assert any(row["journal_kind"] == "reversal" for row in before_upgrade["journals"])
        apply_migration(engine, s["migration"], "upgrade")
        old_installed = False
        assert financial_snapshot(engine) == before_upgrade
        assert protected_definitions(engine) == preserved
        with engine.connect() as connection:
            assert all(
                isinstance(function_definition(connection, name), str) for name in NEW_FUNCTIONS
            )

        # A malformed raw reversal is rejected at deferred commit, before its replacement.
        helpers = runpy.run_path(str(Path(__file__).with_name("test_revision_constraints.py")))
        with pytest.raises(IntegrityError) as rejected:
            with engine.begin() as connection:
                helpers["_revise"](connection, s, active["expense"][0], fault="amount")
        assert rejected.value.orig.diag.constraint_name == "ck_journal_reversal"
        assert (
            rejected.value.orig.diag.message_primary
            == "Reversal lines must be the exact inverse of the original posting"
        )
        assert financial_snapshot(engine) == before_upgrade
        for kind, (operation, body) in active.items():
            corrected = posting.correct_operation(
                owner,
                ledger,
                operation,
                CorrectionCreate(
                    expected_version=2,
                    reason="New fictional correction",
                    replacement=replacement_body(s, kind, body, upgraded=True),
                ),
                "after-correct-" + kind,
            )
            terminal = posting.cancel_operation(
                owner,
                ledger,
                operation,
                CancellationCreate(expected_version=3, reason="New fictional cancel"),
                "after-cancel-" + kind,
            )
            assert (corrected.version, terminal.version, terminal.status) == (3, 4, "cancelled")
            assert terminal.latest_posting == corrected.latest_posting
        # Raw rows bypass service validation; only the unchanged deferred triggers seal them.
        with engine.begin() as connection:
            raw_operation, raw_initial = helpers["_initial"](connection, s, fees=True)
            assert (
                connection.scalar(select(Journal.sealed).where(Journal.id == raw_initial)) is False
            )
        with engine.begin() as connection:
            raw_reversal, raw_current = helpers["_revise"](connection, s, raw_operation)
            assert (
                connection.scalar(
                    select(func.count())
                    .select_from(Journal)
                    .where(Journal.id.in_([raw_reversal, raw_current]), Journal.sealed.is_(False))
                )
                == 2
            )
        with engine.begin() as connection:
            raw_cancel, _ = helpers["_revise"](connection, s, raw_operation, cancel=True)
            assert (
                connection.scalar(select(Journal.sealed).where(Journal.id == raw_cancel)) is False
            )
        current = posting.get_operation(owner, ledger, raw_operation)
        assert (current.version, current.status, current.latest_posting.journal_id) == (
            3,
            "cancelled",
            raw_current,
        )

        before_replay = financial_snapshot(engine)
        assert_exact_reversals(before_replay)
        for method, args, expected in commands:
            assert method(*args).model_dump(mode="json") == expected
        assert financial_snapshot(engine) == before_replay
        apply_migration(engine, s["migration"], "downgrade")
        old_installed = True
        assert protected_definitions(engine) == preserved
        assert financial_snapshot(engine) == before_replay
        with engine.connect() as connection:
            assert (
                function_definition(connection, "coinpup_validate_posting_shape(uuid)") == old_shape
            )
            assert all(function_definition(connection, name) is None for name in NEW_FUNCTIONS)
    finally:
        if old_installed:
            apply_migration(engine, s["migration"], "upgrade")
    assert protected_definitions(engine) == preserved
    assert financial_snapshot(engine) == before_replay
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == original_head
        assert all(isinstance(function_definition(connection, name), str) for name in NEW_FUNCTIONS)


def faulty_journal(connection, s, fault):
    operation, journal = uuid4(), uuid4()
    kind = fault.split("_", 1)[0]
    connection.execute(
        insert(FinancialOperation).values(
            id=operation,
            ledger_id=s["ledger"],
            kind=kind,
            current_journal_id=journal,
            created_by=s["owner"],
        )
    )
    connection.execute(
        insert(Journal).values(
            id=journal,
            operation_id=operation,
            ledger_id=s["ledger"],
            transaction_date=date(2026, 2, 1),
            recognition_date=date(2026, 2, 2)
            if kind in {"transfer", "exchange", "opening"}
            else date(2026, 2, 1),
        )
    )
    rows = []

    def line(role, amount, *, asset="USD", account=None, category=None, component=0):
        rows.append(
            {
                "id": uuid4(),
                "journal_id": journal,
                "ledger_id": s["ledger"],
                "line_no": len(rows) + 1,
                "component_no": component,
                "role": role,
                "asset_id": asset,
                "amount": Decimal(amount),
                "account_id": account,
                "category_id": category,
            }
        )

    if kind == "transfer":
        line("account", "-100", account=s["accounts"][0])
        line("account", "100", account=s["accounts"][1])
    elif kind == "exchange":
        line("account", "-100", account=s["accounts"][0])
        line("exchange", "99")
        line("account", "90", asset="EUR", account=s["accounts"][1])
        line("exchange", "-90", asset="EUR")
    elif kind == "opening":
        line("account", "100", account=s["accounts"][0])
        line("equity", "-100")
    elif kind == "income":
        line("account", "-100", account=s["accounts"][0])
        line("income", "99", category=s["income"][0])
    else:
        line(
            "account",
            "-100" if fault == "expense_principal_fee_balance" else "100",
            account=s["accounts"][0],
        )
        if fault != "expense_principal_fee_balance":
            line("expense", "-100", category=s["categories"][0])
    if kind != "income":
        line("account", "-2", account=s["accounts"][0], component=2)
        line("expense", "2", category=s["categories"][0], component=2)
    connection.execute(insert(JournalLine), rows)
    return journal


def shape_failure(engine, s, fault):
    with pytest.raises(IntegrityError) as rejected:
        with engine.begin() as connection:
            journal = faulty_journal(connection, s, fault)
            connection.execute(
                text("SELECT public.coinpup_validate_posting_shape(:journal)"), {"journal": journal}
            )
    error = rejected.value.orig
    return error.sqlstate, error.diag.constraint_name, error.diag.message_primary


@pytest.mark.parametrize(
    "fault,constraint,message",
    [
        ("expense_principal_fee_balance", "ck_journal_shape", "Initial journal shape is invalid"),
        ("transfer_dates_fee_gap", "ck_journal_shape", "Transfer dates must match"),
        (
            "exchange_balance_dates_fee_gap",
            "ck_journal_balanced",
            "Exchange principal must balance independently in each asset",
        ),
        (
            "expense_signs_fee_gap",
            "ck_journal_fees",
            "Fee components must be contiguous and cannot belong to an opening",
        ),
        ("income_signs_balance", "ck_journal_balanced", "Journal does not balance by asset"),
        (
            "opening_dates_fee_gap",
            "ck_journal_fees",
            "Fee components must be contiguous and cannot belong to an opening",
        ),
    ],
)
def test_overlapping_shape_defects_keep_frozen_first_error(
    dispatch_structure, fault, constraint, message
):
    s = dispatch_structure
    before = financial_snapshot(s["engine"])
    old_installed = False
    try:
        apply_migration(s["engine"], s["migration"], "downgrade")
        old_installed = True
        old = shape_failure(s["engine"], s, fault)
        assert old == ("23514", constraint, message)
        assert financial_snapshot(s["engine"]) == before
        apply_migration(s["engine"], s["migration"], "upgrade")
        old_installed = False
        assert shape_failure(s["engine"], s, fault) == old
        assert financial_snapshot(s["engine"]) == before
    finally:
        if old_installed:
            apply_migration(s["engine"], s["migration"], "upgrade")
