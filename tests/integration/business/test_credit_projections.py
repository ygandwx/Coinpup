"""Read committed originals and rollback-only projections; no revision can yet commit."""

from datetime import date
from decimal import Decimal, localcontext
from uuid import uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from coinpup_api.business.credit_projections import (
    current_credit_lines,
    posting_fact,
    posting_facts,
)
from coinpup_api.business.credit_revision_member_models import (
    BusinessCreditRevisionMember as Member,
)
from coinpup_api.business.credit_revision_models import BusinessCreditRevision as Revision
from coinpup_api.business.settlement_totals import document_totals
from coinpup_api.ledger.service import LedgerError
from sqlalchemy import event, insert, select, text
from sqlalchemy.exc import IntegrityError

from tests.integration.business.test_credit_revision_member_storage import member
from tests.integration.business.test_credit_revision_storage import (
    definitions,
    previous_capacity_definitions,
)
from tests.integration.business.test_credit_revision_storage import revision as revision
from tests.integration.business.test_credit_service import credit, setup_credit
from tests.integration.business.test_draft_service import drafts as drafts
from tests.integration.conftest import credit_projection_migration
from tests.integration.ledger.test_account_classes import snapshot
from tests.integration.ledger.test_posting_service_database import ledger_setup as ledger_setup

pytestmark = pytest.mark.integration


def rows(connection, relation):
    return connection.execute(select(relation).order_by(relation.c.line_no)).mappings().all()


def gated(connection):
    with pytest.raises(IntegrityError) as failure:
        connection.exec_driver_sql(
            "SET CONSTRAINTS tr_business_credit_revisions_complete IMMEDIATE"
        )
    assert failure.value.orig.diag.constraint_name == "ck_credit_revision_shape"


@pytest.mark.parametrize("kind", ["invoice", "bill"])
@pytest.mark.parametrize("count", [1, 100, 200])
def test_real_original_groups_have_one_bounded_exact_read_and_no_writes(drafts, kind, count):
    s = drafts
    _, _, _, request = setup_credit(s, kind, count=count)
    original = credit(s, request)
    source_ids = [item.source_line_id for item in original.postings]
    before = snapshot(s)
    statements = []

    def record(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    with s["engine"].connect() as connection, localcontext() as context:
        context.prec = 2
        event.listen(connection, "before_cursor_execute", record)
        result = rows(connection, current_credit_lines(s["ledger"], source_ids))
        event.remove(connection, "before_cursor_execute", record)
        assert len(statements) == 1 and statements[0].startswith("SELECT")
        assert "pg_advisory" not in statements[0] and "FOR UPDATE" not in statements[0]
        assert len(result) == count
        for row, item in zip(result, original.postings, strict=True):
            assert (row["group_version"], row["operation_version"], row["revision_id"]) == (
                1,
                1,
                None,
            )
            assert row["journal_id"] == item.journal_id
            assert row["net_amount"] == Decimal(item.net_amount)
            assert row["tax_amount"] == Decimal(item.tax_amount)
            assert row["posting_revision_reason"] is None
        assert rows(connection, posting_facts(s["ledger"], credit_ids=[original.id])) == result
        totals = document_totals(connection, s["ledger"], source_ids)
        assert all(
            totals[row["source_line_id"]][1:] == (row["net_amount"], row["tax_amount"])
            for row in result
        )
    assert credit(s, request) == original
    assert snapshot(s) == before


@pytest.mark.parametrize("scope", ["empty", "missing", "other_ledger", "unbounded"])
def test_queries_never_fall_back_to_an_unbounded_or_other_ledger_read(revision, scope):
    s, _, _, original = revision
    with s["engine"].connect() as connection:
        ledger = uuid4() if scope == "other_ledger" else s["ledger"]
        identifiers = [] if scope == "empty" else [uuid4()]
        if scope == "other_ledger":
            identifiers = [original.id]
        relation = posting_facts(ledger, credit_ids=None if scope == "unbounded" else identifiers)
        assert rows(connection, relation) == []
        source_ids = [] if scope == "empty" else [uuid4()]
        if scope == "other_ledger":
            source_ids = [original.postings[0].source_line_id]
        if scope == "unbounded":
            source_ids = None
        assert rows(connection, current_credit_lines(ledger, source_ids)) == []
        journal_id = original.postings[0].journal_id if scope == "other_ledger" else uuid4()
        with pytest.raises(LedgerError) as failure:
            posting_fact(connection, ledger, journal_id)
        assert failure.value.code == "credit_integrity"


@pytest.mark.parametrize(
    "amount", ["0.000000000000000001", "99999999999999999999.999999999999999999"]
)
def test_pending_replacement_projection_preserves_exact_numeric_and_old_journal(revision, amount):
    s, header, _, original = revision
    first = original.postings[0]
    replacement = member(header, original) | dict(net_amount=amount, total_amount=amount)
    before = snapshot(s)
    with s["engine"].connect() as connection:
        transaction = connection.begin()
        connection.execute(insert(Revision).values(**header))
        connection.execute(insert(Member).values(**replacement))
        with localcontext() as context:
            context.prec = 2
            current = rows(connection, current_credit_lines(s["ledger"], [first.source_line_id]))
            assert len(current) == 1
            assert current[0]["net_amount"] == Decimal(amount)
            assert current[0]["group_version"] == current[0]["operation_version"] == 2
            assert current[0]["posting_revision_reason"] == header["revision_reason"]
            assert posting_fact(connection, s["ledger"], first.journal_id)["net_amount"] == Decimal(
                first.net_amount
            )
            assert posting_fact(connection, s["ledger"], replacement["journal_id"]) == current[0]
            assert (
                rows(
                    connection,
                    posting_facts(s["ledger"], journal_ids=[replacement["reversal_journal_id"]]),
                )
                == []
            )
        gated(connection)
        transaction.rollback()
    assert snapshot(s) == before


def test_removed_source_stays_absent_then_rejoins_with_new_identity_before_cancellation(revision):
    s, header, _, original = revision
    first = original.postings[0]
    before = snapshot(s)
    with s["engine"].connect() as connection:
        transaction = connection.begin()
        # These incomplete groups test projection only. The unchanged gate forbids their commit.
        connection.execute(insert(Revision).values(**header))
        connection.execute(insert(Member).values(**member(header, original, "cancel")))
        assert rows(connection, current_credit_lines(s["ledger"], [first.source_line_id])) == []
        next_header = header | dict(
            id=uuid4(),
            group_version=3,
            previous_revision_id=header["id"],
            idempotency_key="fictional-projection-3",
        )
        connection.execute(insert(Revision).values(**next_header))
        replacement = member(next_header, original, "create") | dict(
            operation_id=first.source_operation_id, journal_id=uuid4()
        )
        connection.execute(insert(Member).values(**replacement))
        current = rows(connection, current_credit_lines(s["ledger"], [first.source_line_id]))
        assert len(current) == 1
        assert (current[0]["group_version"], current[0]["operation_version"]) == (3, 1)
        assert current[0]["operation_id"] != first.operation_id
        assert current[0]["posting_revision_reason"] is None
        final = next_header | dict(
            id=uuid4(),
            group_version=4,
            previous_revision_id=next_header["id"],
            idempotency_key="fictional-projection-4",
            action="cancel",
            credit_date=None,
            credit_reason=None,
        )
        connection.execute(insert(Revision).values(**final))
        assert rows(connection, current_credit_lines(s["ledger"], [first.source_line_id])) == []
        assert len(rows(connection, posting_facts(s["ledger"], credit_ids=[original.id]))) == 2
        gated(connection)
        transaction.rollback()
    assert snapshot(s) == before


def test_exact_history_lookup_rejects_cross_table_duplicate_journal(revision):
    s, header, _, original = revision
    before = snapshot(s)
    with s["engine"].connect() as connection:
        transaction = connection.begin()
        connection.execute(insert(Revision).values(**header))
        connection.execute(insert(Member).values(**member(header, original, "create")))
        with pytest.raises(LedgerError) as failure:
            posting_fact(connection, s["ledger"], original.postings[0].journal_id)
        assert failure.value.code == "credit_integrity"
        gated(connection)
        transaction.rollback()
    assert snapshot(s) == before


def test_candidate_scope_includes_new_sources_absent_from_original_credit(drafts):
    s = drafts
    document, _, confirmed, request = setup_credit(s, count=2)
    original = credit(s, request.model_copy(update={"lines": request.lines[:1]}))
    header = dict(
        id=uuid4(),
        ledger_id=s["ledger"],
        credit_id=original.id,
        document_id=document.id,
        group_version=2,
        previous_revision_id=None,
        action="correct",
        revision_reason="Fictional new source projection",
        credit_date=date(2026, 11, 6),
        credit_reason="Fictional replacement",
        created_by=s["owner"],
        idempotency_key="fictional-credit",
    )
    source = confirmed.postings[1]
    replacement = member(header, original, "create") | dict(
        source_line_id=source.line_id,
        source_operation_id=source.operation_id,
        operation_id=source.operation_id,
        journal_id=uuid4(),
    )
    before = snapshot(s)
    with s["engine"].connect() as connection:
        transaction = connection.begin()
        connection.execute(insert(Revision).values(**header))
        connection.execute(insert(Member).values(**replacement))
        current = rows(connection, current_credit_lines(s["ledger"], [source.line_id]))
        assert len(current) == 1 and current[0]["source_line_id"] == source.line_id
        assert current[0]["credit_id"] == original.id
        assert (
            rows(
                connection, current_credit_lines(s["ledger"], [original.postings[0].source_line_id])
            )
            == []
        )
        # Current capacity now shares the projection, but the candidate still cannot commit.
        assert document_totals(connection, s["ledger"], [source.line_id]) == {
            source.line_id: (Decimal("0"), Decimal("1"), Decimal("0"))
        }
        gated(connection)
        transaction.rollback()
    assert snapshot(s) == before


def test_multiple_original_groups_do_not_multiply_or_hide_shared_source_amounts(revision):
    s, _, request, original = revision
    second = credit(s, request.model_copy(update={"id": uuid4()}), key="fictional-second-credit")
    with s["engine"].connect() as connection:
        result = rows(
            connection, current_credit_lines(s["ledger"], [original.postings[0].source_line_id])
        )
        assert len(result) == 2 and {row["credit_id"] for row in result} == {original.id, second.id}
        assert all(row["net_amount"] == Decimal("17.23") for row in result)


def test_stable_projection_sees_later_same_transaction_writes_in_a_deferred_trigger(revision):
    s, header, _, original = revision
    first = original.postings[0]
    replacement = member(header, original)
    before = snapshot(s)
    with s["engine"].connect() as connection:
        transaction = connection.begin()
        connection.exec_driver_sql("""
            CREATE TEMP TABLE fictional_projection_probe(ledger_id uuid, source_id uuid)
                ON COMMIT DROP;
            CREATE TEMP TABLE fictional_projection_seen(group_version int) ON COMMIT DROP;
            CREATE FUNCTION pg_temp.fictional_projection_check() RETURNS trigger
            LANGUAGE plpgsql AS $$ DECLARE item record; BEGIN
                SELECT * INTO STRICT item FROM public.coinpup_current_credit_lines(
                    NEW.ledger_id, ARRAY[NEW.source_id]);
                IF item.group_version IS DISTINCT FROM 2 OR item.net_amount IS DISTINCT FROM 1
                    THEN RAISE EXCEPTION 'Fictional projection did not see pending rows'; END IF;
                INSERT INTO pg_temp.fictional_projection_seen VALUES (item.group_version);
                RETURN NULL;
            END; $$;
            CREATE CONSTRAINT TRIGGER fictional_projection_complete AFTER INSERT
                ON pg_temp.fictional_projection_probe DEFERRABLE INITIALLY DEFERRED
                FOR EACH ROW EXECUTE FUNCTION pg_temp.fictional_projection_check();
        """)
        connection.execute(
            text("INSERT INTO pg_temp.fictional_projection_probe VALUES (:l,:s)"),
            dict(l=s["ledger"], s=first.source_line_id),
        )
        connection.execute(insert(Revision).values(**header))
        connection.execute(insert(Member).values(**replacement))
        connection.exec_driver_sql("SET CONSTRAINTS fictional_projection_complete IMMEDIATE")
        assert connection.exec_driver_sql(
            "SELECT group_version FROM pg_temp.fictional_projection_seen"
        ).scalars().all() == [2]
        gated(connection)
        transaction.rollback()
    assert snapshot(s) == before


def test_empty_projection_migration_keeps_original_sql_and_receipts_exact(revision):
    s, _, request, original = revision
    before = snapshot(s)
    with (
        s["engine"].begin() as connection,
        Operations.context(MigrationContext.configure(connection)),
    ):
        functions, triggers = definitions(connection)
        previous, previous_triggers = previous_capacity_definitions(connection)
        assert (
            connection.exec_driver_sql(
                "SELECT provolatile, prosecdef, proconfig FROM pg_proc p "
                "JOIN pg_namespace n ON n.oid=p.pronamespace WHERE n.nspname='public' "
                "AND p.proname IN ('coinpup_credit_posting_facts','coinpup_current_credit_lines')"
            ).all()
            == [("s", False, ["search_path=pg_catalog"])] * 2
        )
        migration = credit_projection_migration()
        migration["downgrade"]()
        remaining, old_triggers = definitions(connection)
        assert remaining == [
            body
            for body in previous
            if not any(
                name in body.split("\n", 1)[0]
                for name in (
                    "coinpup_validate_credit_origin_facts",
                    "coinpup_validate_credit_group_transitions",
                    "coinpup_business_credit_revision_response",
                    "coinpup_validate_credit_revision_receipt",
                    "coinpup_validate_credit_posting_fact",
                    "coinpup_credit_posting_facts",
                    "coinpup_current_credit_lines",
                )
            )
        ]
        assert old_triggers == previous_triggers
        migration["upgrade"]()
        assert definitions(connection) == (functions, triggers)
    assert credit(s, request) == original
    assert snapshot(s) == before


def test_projection_downgrade_refuses_pending_revision_history(revision):
    s, header, _, _ = revision
    before = snapshot(s)
    with pytest.raises(IntegrityError) as failure, s["engine"].begin() as connection:
        connection.execute(insert(Revision).values(**header))
        with Operations.context(MigrationContext.configure(connection)):
            credit_projection_migration()["downgrade"]()
    assert failure.value.orig.diag.constraint_name == "ck_credit_revision_downgrade"
    assert snapshot(s) == before
