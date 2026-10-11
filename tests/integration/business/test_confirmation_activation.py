"""Real PostgreSQL accruals use only fictional documents and permanent receipts."""

from datetime import date
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from coinpup_api.business.confirmation_models import BusinessConfirmation, BusinessPosting
from coinpup_api.business.models import BusinessDocument, BusinessDocumentLine
from coinpup_api.business.pricing import PriceInput
from coinpup_api.ledger.models import CommandReceipt, Journal, JournalLine
from coinpup_api.ledger.posting_schemas import CancellationCreate
from coinpup_api.ledger.service import LedgerError
from sqlalchemy import delete, insert, select, update
from sqlalchemy.exc import IntegrityError

from tests.integration.business.confirmation_fixture import confirm_rows, source
from tests.integration.business.test_draft_lines import line, save
from tests.integration.conftest import business_accrual_migration
from tests.integration.ledger.test_account_classes import snapshot
from tests.integration.ledger.test_period_transactions import change as change_period
from tests.integration.ledger.test_posting_service_database import ledger_setup as ledger_setup

pytestmark = pytest.mark.integration


def extra_line(s, parent, rows, *, zero=False):
    row = line(
        s,
        parent,
        PriceInput("1", "0" if zero else "50", tax_rate_percent="10"),
        line_no=len(rows) + 1,
        recognition_date=date(2026, 8, 31) if zero else date(2026, 10, 1),
        category_kind=rows[0]["category_kind"],
        category_id=rows[0]["category_id"],
    )
    save(s, row)
    rows.append(row)


@pytest.mark.parametrize("kind", ["invoice", "bill"])
def test_confirmed_gross_accruals_keep_dates_zero_source_and_balanced_control(ledger_setup, kind):
    s = ledger_setup
    parent, header, rows, account = source(s, kind)
    extra_line(s, parent, rows)
    extra_line(s, parent, rows, zero=True)
    with s["engine"].begin() as c:
        result = confirm_rows(c, s, parent, header, rows, account)
    with s["engine"].connect() as c:
        assert c.scalar(select(BusinessDocument.state)) == "confirmed"
        assert c.scalar(select(CommandReceipt.response)) == result
        proof = c.scalar(select(BusinessConfirmation.snapshot))
        assert len(proof["lines"]) == 3 and len(result["postings"]) == 2
        assert Decimal(proof["lines"][0]["tax_amount"]) == Decimal("10")
        journals = c.execute(select(Journal.__table__)).mappings().all()
        assert {j["recognition_date"] for j in journals} == {date(2026, 9, 30), date(2026, 10, 1)}
        assert all(j["sealed"] and j["transaction_date"] == header["issue_date"] for j in journals)
        postings = c.execute(select(JournalLine.__table__)).mappings().all()
        expected = Decimal("165") if kind == "invoice" else Decimal("-165")
        assert sum(p["amount"] for p in postings if p["role"] == "account") == expected
        assert sum(p["amount"] for p in postings) == 0
        assert all(p["account_id"] != s["account"].id for p in postings)


@pytest.mark.parametrize(
    "case", ["net_only", "party", "source_line", "fee", "date", "missing_line"]
)
def test_wrong_balanced_shape_or_incomplete_group_rolls_back(ledger_setup, case):
    s = ledger_setup
    parent, header, rows, account = source(s)
    extra_line(s, parent, rows)
    before = snapshot(s)
    if case == "date":
        header = header | {"issue_date": date(2026, 10, 10)}
    if case == "missing_line":
        rows = rows[:1]

    def change(parameters, row):
        if case == "net_only":
            parameters[0]["amount"] = row["net_amount"]
            parameters[1]["amount"] = -row["net_amount"]
        elif case in {"party", "source_line"}:
            field = "party_id" if case == "party" else "document_line_id"
            for p in parameters:
                p[field] = None
        elif case == "fee":
            parameters.extend(
                [
                    parameters[0]
                    | {"id": uuid4(), "line_no": 3, "component_no": 1, "amount": Decimal("-1")},
                    parameters[1]
                    | {
                        "id": uuid4(),
                        "line_no": 4,
                        "component_no": 1,
                        "role": "expense",
                        "category_id": s["expenses"][0].id,
                        "amount": Decimal("1"),
                    },
                ]
            )

    with pytest.raises(IntegrityError) as error, s["engine"].begin() as c:
        confirm_rows(c, s, parent, header, rows, account, change=change)
    assert error.value.orig.diag.constraint_name in {
        "ck_business_accrual_shape",
        "ck_business_confirmation_shape",
    }
    assert snapshot(s) == before


@pytest.mark.parametrize("case", ["header", "line", "anonymous_line", "confirmation", "membership"])
def test_confirmed_source_and_membership_cannot_be_edited(ledger_setup, case):
    s = ledger_setup
    parent, header, rows, account = source(s)
    with s["engine"].begin() as c:
        confirm_rows(c, s, parent, header, rows, account)
    before = snapshot(s)
    statements = {
        "header": update(BusinessDocument).values(version=3, notes="Fictional replacement"),
        "line": update(BusinessDocumentLine).values(version=2, description="Fictional replacement"),
        "anonymous_line": insert(BusinessDocumentLine).values(
            id=uuid4(), ledger_id=s["ledger"], document_id=parent
        ),
        "confirmation": delete(BusinessConfirmation),
        "membership": update(BusinessPosting).values(journal_id=uuid4()),
    }
    with pytest.raises(IntegrityError), s["engine"].begin() as c:
        c.execute(statements[case])
    assert snapshot(s) == before


def test_zero_line_in_closed_period_blocks_whole_confirmation_until_explicit_reopen(ledger_setup):
    s = ledger_setup
    parent, header, rows, account = source(s)
    extra_line(s, parent, rows, zero=True)
    change_period(s, cutoff="2026-08-31")
    before = snapshot(s)
    with pytest.raises(IntegrityError) as error, s["engine"].begin() as c:
        confirm_rows(c, s, parent, header, rows, account)
    assert error.value.orig.diag.constraint_name == "ck_journal_period_closed"
    assert snapshot(s) == before
    change_period(s, version=2, cutoff=None, action="reopen")
    with s["engine"].begin() as c:
        confirm_rows(c, s, parent, header, rows, account)


def test_legacy_reads_and_commands_do_not_parse_or_edit_new_accruals(ledger_setup):
    from coinpup_api.business.drafts import DraftService

    s = ledger_setup
    parent, header, rows, account = source(s)
    with s["engine"].begin() as c:
        result = confirm_rows(c, s, parent, header, rows, account)
    assert s["posting"].list_operations(s["owner"], s["ledger"]) == []
    assert DraftService(s["engine"]).list_drafts(s["owner"], s["ledger"]) == []
    operation = UUID(result["postings"][0]["operation_id"])
    before = snapshot(s)
    for action in [
        lambda: s["posting"].get_operation(s["owner"], s["ledger"], operation),
        lambda: s["posting"].history(s["owner"], s["ledger"], operation),
        lambda: s["posting"].cancel_operation(
            s["owner"],
            s["ledger"],
            operation,
            CancellationCreate(expected_version=1, reason="Fictional cancellation"),
            "fictional-standalone",
        ),
    ]:
        with pytest.raises(LedgerError) as error:
            action()
        assert error.value.status == 404
    assert snapshot(s) == before


def test_activation_round_trip_and_history_refuses_downgrade(ledger_setup):
    s = ledger_setup
    m = business_accrual_migration()
    with s["engine"].begin() as c, Operations.context(MigrationContext.configure(c)):
        m["downgrade"]()
        m["upgrade"]()
    parent, header, rows, account = source(s)
    with s["engine"].begin() as c:
        confirm_rows(c, s, parent, header, rows, account)
    before = snapshot(s)
    with pytest.raises(IntegrityError) as error, s["engine"].begin() as c:
        with Operations.context(MigrationContext.configure(c)):
            m["downgrade"]()
    assert error.value.orig.diag.constraint_name == "ck_business_accrual_downgrade"
    assert snapshot(s) == before


@pytest.mark.parametrize("case", ["missing", "reordered"])
def test_receipt_must_preserve_every_member_and_the_first_line_anchor(ledger_setup, case):
    s = ledger_setup
    parent, header, rows, account = source(s)
    extra_line(s, parent, rows)
    before = snapshot(s)

    def change_receipt(result):
        if case == "missing":
            result["postings"].pop()
        else:
            result["postings"].reverse()

    with pytest.raises(IntegrityError) as error, s["engine"].begin() as c:
        confirm_rows(c, s, parent, header, rows, account, receipt_change=change_receipt)
    assert error.value.orig.diag.constraint_name == "ck_business_confirmation_shape"
    assert snapshot(s) == before


def test_even_a_complete_balanced_standalone_reversal_cannot_cancel_a_child(ledger_setup):
    from types import SimpleNamespace

    from coinpup_api.ledger.assets import ASSETS
    from coinpup_api.ledger.models import FinancialOperation
    from coinpup_api.ledger.posting_storage import prepare_reversal_lines

    s = ledger_setup
    parent, header, rows, account = source(s)
    with s["engine"].begin() as c:
        result = confirm_rows(c, s, parent, header, rows, account)
    operation = UUID(result["postings"][0]["operation_id"])
    original = UUID(result["postings"][0]["journal_id"])
    with s["engine"].connect() as c:
        saved = [
            SimpleNamespace(**row) for row in c.execute(select(JournalLine.__table__)).mappings()
        ]
    reversal = uuid4()
    _, parameters = prepare_reversal_lines(saved, reversal, ASSETS)
    before = snapshot(s)
    with pytest.raises(IntegrityError) as error, s["engine"].begin() as c:
        c.exec_driver_sql("SELECT pg_advisory_xact_lock(18945999704708432)")
        c.execute(
            insert(Journal).values(
                id=reversal,
                operation_id=operation,
                ledger_id=s["ledger"],
                operation_version=2,
                journal_kind="reversal",
                reverses_journal_id=original,
                transaction_date=header["issue_date"],
                recognition_date=rows[0]["recognition_date"],
                description=rows[0]["description"],
                revision_reason="Fictional standalone reversal",
            )
        )
        c.execute(insert(JournalLine), parameters)
        c.execute(
            update(FinancialOperation)
            .where(FinancialOperation.id == operation)
            .values(version=2, status="cancelled")
        )
    assert error.value.orig.diag.constraint_name == "ck_business_accrual_shape"
    assert snapshot(s) == before
