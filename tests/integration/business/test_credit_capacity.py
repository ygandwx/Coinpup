"""Exercise credit capacity in rollback-only transactions while credit writes remain gated."""

from datetime import date
from decimal import Decimal
from uuid import uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from coinpup_api.business.confirmation_models import BusinessPosting
from coinpup_api.business.models import BusinessDocument
from coinpup_api.business.settlement_balances import BusinessSettlementBalances
from coinpup_api.business.settlement_totals import applied_totals, document_totals
from sqlalchemy import event, select, text
from sqlalchemy.exc import IntegrityError

from tests.integration.business.test_credit_storage import pending
from tests.integration.business.test_draft_service import drafts as drafts
from tests.integration.business.test_settlement_service import allocate, pay, prepared
from tests.integration.conftest import credit_capacity_migration
from tests.integration.ledger.test_account_classes import snapshot
from tests.integration.ledger.test_posting_service_database import ledger_setup as ledger_setup

pytestmark = pytest.mark.integration


def pending_credit(s, connection, group, amount):
    source = group.lines[0]
    original = (
        connection.execute(
            select(BusinessPosting).where(
                BusinessPosting.document_id == source.document_id,
                BusinessPosting.line_id == source.document_line_id,
            )
        )
        .mappings()
        .one()
    )
    header = dict(
        id=uuid4(),
        ledger_id=s["ledger"],
        document_id=source.document_id,
        source_version=connection.scalar(
            select(BusinessDocument.version).where(BusinessDocument.id == source.document_id)
        ),
        credit_date=date(2026, 10, 21),
        reason="Fictional capacity-only rollback probe",
        created_by=s["owner"],
        idempotency_key="fictional-pending-credit",
    )
    member = dict(
        id=uuid4(),
        ledger_id=s["ledger"],
        credit_id=header["id"],
        document_id=source.document_id,
        source_line_id=source.document_line_id,
        source_operation_id=original["operation_id"],
        line_no=1,
        net_amount=Decimal(amount),
        tax_amount=Decimal("0"),
        total_amount=Decimal(amount),
        recognition_date=date(2026, 10, 21),
        operation_id=original["operation_id"],
        journal_id=original["journal_id"],
    )
    pending(connection, header, member)


@pytest.mark.parametrize("direction", ["receipt", "payout"])
@pytest.mark.parametrize("amount,exceeds", [("40", False), ("40.01", True)])
def test_database_allocation_cannot_use_gross_capacity_reserved_by_credit(
    drafts, direction, amount, exceeds
):
    s = drafts
    body, group = prepared(s, direction=direction, initial=False)
    payment = pay(s, body)
    source = group.lines[0].model_copy(update={"payment_amount": "60", "document_amount": "60"})
    group = group.model_copy(update={"lines": [source]})
    saved = allocate(s, group)
    before = snapshot(s)
    with s["engine"].connect() as connection:
        transaction = connection.begin()
        try:
            pending_credit(s, connection, group, amount)
            assert document_totals(connection, s["ledger"], [source.document_line_id]) == {
                source.document_line_id: (Decimal("60"), Decimal(amount), Decimal("0"))
            }
            query = text("SELECT coinpup_validate_business_allocation(:id)")
            if exceeds:
                with pytest.raises(IntegrityError) as failure:
                    connection.execute(query, {"id": saved.id})
                assert failure.value.orig.diag.constraint_name == "ck_business_allocation_shape"
            else:
                connection.execute(query, {"id": saved.id})
                # The pending shape must still fail; these probes never commit a fake credit.
                with pytest.raises(IntegrityError) as failure:
                    connection.exec_driver_sql(
                        "SET CONSTRAINTS tr_business_credits_complete IMMEDIATE"
                    )
                assert failure.value.orig.diag.constraint_name == "ck_business_credit_shape"
        finally:
            transaction.rollback()
    assert snapshot(s) == before
    assert pay(s, body) == payment
    assert allocate(s, group) == saved
    assert snapshot(s) == before


@pytest.mark.parametrize(
    "applied,outstanding", [("0.00", "100.00"), ("60.00", "40.00"), ("100.00", "0.00")]
)
def test_no_credit_settlement_json_is_unchanged_and_query_count_stays_bounded(
    drafts, monkeypatch, applied, outstanding
):
    s = drafts
    body, group = prepared(s, initial=False)
    pay(s, body)
    source = group.lines[0]
    if applied != "0.00":
        allocate(
            s,
            group.model_copy(
                update={
                    "lines": [
                        source.model_copy(
                            update={"payment_amount": applied, "document_amount": applied}
                        )
                    ]
                }
            ),
        )
    reads = BusinessSettlementBalances(s["engine"])
    statements = []

    def record(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    event.listen(s["engine"], "before_cursor_execute", record)
    try:
        result = reads.document_capacity(s["owner"], s["ledger"], source.document_id)
    finally:
        event.remove(s["engine"], "before_cursor_execute", record)
    assert result.model_dump(mode="json") == dict(
        id=str(source.document_id),
        ledger_id=str(s["ledger"]),
        party_id=str(s["party"].id),
        asset_id="USD",
        total_amount="100.00",
        applied_amount=applied,
        outstanding_amount=outstanding,
        lines=[
            dict(
                id=str(source.document_line_id),
                line_no=1,
                project_id=str(s["project"].id),
                total_amount="100.00",
                applied_amount=applied,
                outstanding_amount=outstanding,
            )
        ],
    )
    current_selects = [sql for sql in statements if sql.startswith("SELECT")]
    # The shared owner lookup was present before this migration: one authentication
    # SELECT plus five business SELECTs, not five SELECTs for the complete request.
    assert len(current_selects) == 6
    assert sum("FROM administrators " in sql for sql in current_selects) == 1
    assert not any("pg_advisory" in sql or "command_receipts" in sql for sql in statements)

    def previous_totals(session, ledger_id, identifiers):
        return {
            identifier: (amount, Decimal(0), Decimal(0))
            for identifier, amount in applied_totals(
                session, ledger_id, identifiers, by_document_line=True
            ).items()
        }

    # Exercise the unchanged pre-credit aggregate, rather than widening the query
    # budget without a measured baseline. Its zero credit components are explicit.
    monkeypatch.setattr("coinpup_api.business.settlement_balances.document_totals", previous_totals)
    statements.clear()
    event.listen(s["engine"], "before_cursor_execute", record)
    try:
        previous = reads.document_capacity(s["owner"], s["ledger"], source.document_id)
    finally:
        event.remove(s["engine"], "before_cursor_execute", record)
    previous_selects = [sql for sql in statements if sql.startswith("SELECT")]
    assert previous.model_dump(mode="json") == result.model_dump(mode="json")
    assert len(previous_selects) == len(current_selects) == 6
    assert previous_selects[:-1] == current_selects[:-1]
    assert "business_credit_lines" not in previous_selects[-1]
    assert "coinpup_current_credit_lines" in current_selects[-1]


def test_capacity_migration_empty_roundtrip_restores_exact_refund_validator(drafts):
    s = drafts
    migration = credit_capacity_migration()
    body, group = prepared(s)
    saved = pay(s, body)
    before = snapshot(s)
    with (
        s["engine"].begin() as connection,
        Operations.context(MigrationContext.configure(connection)),
    ):
        migration["downgrade"]()
        old = connection.scalar(
            text(
                "SELECT pg_get_functiondef("
                "'coinpup_validate_business_allocation(uuid)'::regprocedure)"
            )
        )
        assert "business_credit_lines" not in old and "business_refunds" in old
        migration["upgrade"]()
        current = connection.scalar(
            text(
                "SELECT pg_get_functiondef("
                "'coinpup_validate_business_allocation(uuid)'::regprocedure)"
            )
        )
        assert "coinpup_current_credit_lines" in current and "business_refunds" in current
    assert snapshot(s) == before
    assert pay(s, body) == saved
    assert snapshot(s) == before


def test_capacity_downgrade_rejects_credit_notification_history_without_rows(drafts):
    s = drafts
    migration = credit_capacity_migration()
    before = snapshot(s)
    with pytest.raises(IntegrityError) as failure, s["engine"].begin() as connection:
        assert connection.scalar(text("SELECT count(*) FROM public.business_credits")) == 0
        assert connection.scalar(text("SELECT count(*) FROM public.business_credit_lines")) == 0
        connection.exec_driver_sql(
            "CREATE TEMP TABLE business_credits(id uuid, ledger_id uuid, version int) "
            "ON COMMIT DROP; "
            "CREATE TRIGGER fictional_credit_capacity_history AFTER INSERT "
            "ON pg_temp.business_credits FOR EACH ROW "
            "EXECUTE FUNCTION public.coinpup_record_change()"
        )
        connection.execute(
            text("INSERT INTO pg_temp.business_credits VALUES (:id,:ledger,1)"),
            {"id": uuid4(), "ledger": s["ledger"]},
        )
        with Operations.context(MigrationContext.configure(connection)):
            migration["downgrade"]()
    assert failure.value.orig.diag.constraint_name == "ck_business_credit_downgrade"
    assert snapshot(s) == before
