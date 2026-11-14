"""A refund operation belongs to exactly one stable refund, including at deferred commit."""

from uuid import uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from coinpup_api.business.refund_revision_models import BusinessRefundRevision as Revision
from coinpup_api.ledger.models import FinancialOperation
from coinpup_api.sync.locking import acquire_write_lock
from coinpup_api.sync.models import ChangeLog
from sqlalchemy import insert, select, text, update
from sqlalchemy.exc import IntegrityError

from tests.integration.business.test_credit_fact_validation import copy_posting, stored
from tests.integration.business.test_credit_revision_storage import definitions
from tests.integration.business.test_draft_service import drafts as drafts
from tests.integration.business.test_refund_revision_headers import replacement
from tests.integration.business.test_refund_revision_headers import revision as revision
from tests.integration.business.test_refund_service import refund
from tests.integration.conftest import refund_revision_guards_migration
from tests.integration.ledger.test_account_classes import snapshot
from tests.integration.ledger.test_posting_service_database import ledger_setup as ledger_setup

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("action", ["correct", "cancel"])
def test_stable_refund_events_and_revision_ids_keep_the_original_sealed(revision, action):
    s, header, body, original = revision
    before = snapshot(s)
    with s["engine"].connect() as c:
        first = header | replacement(body) | {"idempotency_key": "fictional-first-correction"}
        second = (
            header
            | (replacement(body) if action == "correct" else {})
            | dict(
                id=uuid4(),
                previous_revision_id=first["id"],
                operation_version=3,
                previous_journal_id=first["replacement_journal_id"],
                reversal_journal_id=uuid4(),
                idempotency_key="fictional-second-revision",
            )
        )
        c.execute(insert(Revision).values(**first))
        c.execute(insert(Revision).values(**second))
        rows = c.execute(
            select(
                ChangeLog.entity_version,
                ChangeLog.change_kind,
                ChangeLog.owner_id,
                ChangeLog.ledger_id,
            )
            .where(
                ChangeLog.entity_type == "business_refunds", ChangeLog.entity_id == str(original.id)
            )
            .order_by(ChangeLog.seq)
        ).all()
        assert rows == [
            (1, "upsert", s["owner"], s["ledger"]),
            (2, "upsert", s["owner"], s["ledger"]),
            (3, "cancel" if action == "cancel" else "upsert", s["owner"], s["ledger"]),
        ]
        own = c.execute(
            select(ChangeLog.entity_id, ChangeLog.entity_version)
            .where(ChangeLog.entity_type == "business_refund_revisions")
            .order_by(ChangeLog.seq)
        ).all()
        assert own == [(str(first["id"]), 1), (str(second["id"]), 1)]
        assert (
            c.scalar(text("SELECT version FROM business_refunds WHERE id=:id"), {"id": original.id})
            == 1
        )
        with pytest.raises(IntegrityError) as rejected:
            c.exec_driver_sql("SET CONSTRAINTS tr_business_refund_revisions_complete IMMEDIATE")
        assert rejected.value.orig.diag.constraint_name == "ck_refund_revision_shape"
    assert refund(s, body) == original and snapshot(s) == before


@pytest.mark.parametrize("mode", ["unowned", "head_only", "matching_reversal"])
def test_specific_deferred_guard_rejects_unowned_or_isolated_operation_updates(revision, mode):
    s, _, body, original = revision
    before = snapshot(s)
    with s["engine"].connect() as c:
        acquire_write_lock(c)
        if mode == "unowned":
            c.execute(
                insert(FinancialOperation).values(
                    **(stored(c, FinancialOperation, original.operation_id) | dict(id=uuid4()))
                )
            )
        else:
            c.execute(
                update(FinancialOperation)
                .where(FinancialOperation.id == original.operation_id)
                .values(version=2, status="cancelled")
            )
            if mode == "matching_reversal":
                copy_posting(
                    c,
                    original.journal_id,
                    dict(
                        id=uuid4(),
                        operation_version=2,
                        journal_kind="reversal",
                        reverses_journal_id=original.journal_id,
                        revision_reason="Fictional isolated cancel",
                    ),
                    reverse=True,
                )
        with pytest.raises(IntegrityError) as rejected:
            c.exec_driver_sql("SET CONSTRAINTS zz_tr_refund_operations_complete IMMEDIATE")
        assert rejected.value.orig.diag.constraint_name == (
            "ck_refund_revision_shape" if mode == "unowned" else "ck_business_refund_shape"
        )
    assert refund(s, body) == original and snapshot(s) == before


def test_guard_migration_round_trip_preserves_all_other_functions_triggers_and_receipt(revision):
    s, _, body, original = revision
    before = snapshot(s)
    functions = {"coinpup_check_refund_operation_owner", "coinpup_record_refund_revision_change"}
    triggers = {"zz_tr_refund_operations_complete", "trg_business_refund_revision_change_log"}
    with s["engine"].begin() as c, Operations.context(MigrationContext.configure(c)):
        current = definitions(c)
        assert c.exec_driver_sql(
            "SELECT tgdeferrable,tginitdeferred,tgenabled FROM pg_trigger "
            "WHERE tgrelid='public.financial_operations'::regclass "
            "AND tgname='zz_tr_refund_operations_complete'"
        ).one() == (True, True, "O")
        migration = refund_revision_guards_migration()
        migration["downgrade"]()
        previous = definitions(c)
        assert previous[0] == [
            sql
            for sql in current[0]
            if not any(name in sql.split("\n", 1)[0] for name in functions)
        ]
        assert previous[1] == [
            sql for sql in current[1] if not any(name in sql for name in triggers)
        ]
        migration["upgrade"]()
        assert definitions(c) == current
    assert refund(s, body) == original and snapshot(s) == before


@pytest.mark.parametrize("notification_only", [False, True])
def test_guard_downgrade_refuses_stable_notifications_or_revision_records(
    revision, notification_only
):
    s, header, _, original = revision
    before = snapshot(s)
    with s["engine"].connect() as c, Operations.context(MigrationContext.configure(c)):
        if notification_only:
            c.exec_driver_sql(
                "CREATE TEMP TABLE business_refunds(id uuid,ledger_id uuid,version int) "
                "ON COMMIT DROP; "
                "CREATE TRIGGER fictional_stable_refund AFTER INSERT ON pg_temp.business_refunds "
                "FOR EACH ROW EXECUTE FUNCTION public.coinpup_record_change()"
            )
            c.execute(
                text("INSERT INTO pg_temp.business_refunds VALUES (:id,:ledger,2)"),
                {"id": original.id, "ledger": s["ledger"]},
            )
        else:
            c.execute(insert(Revision).values(**header))
        with pytest.raises(IntegrityError) as caught:
            refund_revision_guards_migration()["downgrade"]()
        assert caught.value.orig.diag.constraint_name == "ck_refund_revision_downgrade"
    assert snapshot(s) == before
