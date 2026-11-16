"""Real operation ownership and stable group notifications precede the complete write gate."""

from uuid import uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from coinpup_api.business.allocation_return_revision_member_models import (
    BusinessReturnRevisionMember as Member,
)
from coinpup_api.business.allocation_return_revision_models import (
    BusinessAllocationReturnRevision as Revision,
)
from coinpup_api.ledger.models import FinancialOperation
from coinpup_api.sync.locking import acquire_write_lock
from coinpup_api.sync.models import ChangeLog
from sqlalchemy import insert, select, text, update
from sqlalchemy.exc import IntegrityError

from tests.integration.business.test_allocation_return_historical_validation import (
    stage,
)
from tests.integration.business.test_allocation_return_revision_headers import (
    revision as revision,
)
from tests.integration.business.test_allocation_return_revision_members import member
from tests.integration.business.test_allocation_return_service import submit_return
from tests.integration.business.test_credit_fact_validation import copy_posting, stored
from tests.integration.business.test_credit_revision_storage import definitions
from tests.integration.business.test_draft_service import drafts as drafts
from tests.integration.conftest import return_ownership_migration
from tests.integration.ledger.test_account_classes import snapshot
from tests.integration.ledger.test_posting_service_database import (
    ledger_setup as ledger_setup,
)

pytestmark = pytest.mark.integration


def check(c, op):
    c.execute(text("SELECT coinpup_validate_return_operation_owner(:id)"), {"id": op})


@pytest.mark.parametrize("action", ["correct", "cancel"])
def test_versioned_group_events_keep_stable_and_revision_identities_separate(revision, action):
    s, header, body, orig = revision
    last = header | dict(
        id=uuid4(),
        group_version=3,
        previous_revision_id=header["id"],
        idempotency_key="fictional-guard-next",
        action=action,
    )
    if action == "cancel":
        last.update(return_date=None, return_reason=None)
    before = snapshot(s)
    with s["engine"].connect() as c:
        c.execute(insert(Revision).values(**header))
        c.execute(insert(Revision).values(**last))
        events = (
            c.execute(
                select(ChangeLog)
                .where(
                    ChangeLog.entity_type == "business_allocation_returns",
                    ChangeLog.entity_id == str(orig.id),
                )
                .order_by(ChangeLog.seq)
            )
            .mappings()
            .all()
        )
        assert [(r["entity_version"], r["change_kind"]) for r in events] == [
            (1, "upsert"),
            (2, "upsert"),
            (3, "cancel" if action == "cancel" else "upsert"),
        ]
        assert all(r["owner_id"] == s["owner"] and r["ledger_id"] == s["ledger"] for r in events)
        individual = c.execute(
            select(ChangeLog.entity_id, ChangeLog.entity_version)
            .where(ChangeLog.entity_type == "business_return_revisions")
            .order_by(ChangeLog.seq)
        ).all()
        assert individual == [(str(header["id"]), 1), (str(last["id"]), 1)]
        with pytest.raises(IntegrityError) as rejected:
            c.exec_driver_sql("SET CONSTRAINTS tr_business_return_revisions_complete IMMEDIATE")
        assert rejected.value.orig.diag.constraint_name == "ck_return_revision_shape"
        c.rollback()
    assert submit_return(s, body) == orig and snapshot(s) == before


@pytest.mark.parametrize("fault", ["orphan", "head-only", "balanced-reversal"])
def test_standalone_ledger_progress_cannot_acquire_a_return_operation(revision, fault):
    s, _, _, orig = revision
    old = orig.postings[0]
    before = snapshot(s)
    with s["engine"].connect() as c:
        acquire_write_lock(c)
        if fault == "orphan":
            op = stored(c, FinancialOperation, old.operation_id) | dict(
                id=uuid4(), current_journal_id=uuid4()
            )
            c.execute(insert(FinancialOperation).values(**op))
        else:
            c.execute(
                update(FinancialOperation)
                .where(FinancialOperation.id == old.operation_id)
                .values(version=2, status="cancelled")
            )
            if fault == "balanced-reversal":
                copy_posting(
                    c,
                    old.journal_id,
                    dict(
                        id=uuid4(),
                        operation_version=2,
                        journal_kind="reversal",
                        reverses_journal_id=old.journal_id,
                        revision_reason="Fictional standalone cancel",
                    ),
                    reverse=True,
                )
        with pytest.raises(IntegrityError) as rejected:
            c.exec_driver_sql("SET CONSTRAINTS zz_tr_return_operations_complete IMMEDIATE")
        assert rejected.value.orig.diag.constraint_name == "ck_return_revision_shape"
        c.rollback()
    assert snapshot(s) == before


def test_queued_head_events_read_the_final_cancelled_operation_and_last_posting(
    revision,
):
    s, h, _, orig = revision
    old = orig.postings[0]
    before = snapshot(s)
    with s["engine"].connect() as c:
        check(c, old.operation_id)
        retained = stage(c, revision)
        second = h | dict(
            id=uuid4(),
            group_version=3,
            previous_revision_id=h["id"],
            action="cancel",
            return_date=None,
            return_reason=None,
            idempotency_key="fictional-guard-end",
        )
        c.execute(insert(Revision).values(**second))
        c.execute(
            insert(Member).values(
                **(
                    retained
                    | dict(
                        id=uuid4(),
                        revision_id=second["id"],
                        action="cancel",
                        previous_operation_version=2,
                        operation_version=3,
                        previous_journal_id=retained["journal_id"],
                        reversal_journal_id=uuid4(),
                        line_no=None,
                        journal_id=None,
                        payment_amount=None,
                        document_amount=None,
                        absorbs_remainder=None,
                    )
                )
            )
        )
        c.execute(
            update(FinancialOperation)
            .where(FinancialOperation.id == old.operation_id)
            .values(version=3, status="cancelled")
        )
        c.exec_driver_sql("SET CONSTRAINTS zz_tr_return_operations_complete IMMEDIATE")
        check(c, old.operation_id)
        with pytest.raises(IntegrityError) as rejected:
            c.exec_driver_sql("SET CONSTRAINTS tr_business_return_revisions_complete IMMEDIATE")
        assert rejected.value.orig.diag.constraint_name == "ck_return_revision_shape"
        c.rollback()
    assert snapshot(s) == before


def test_first_reserved_operation_cannot_be_claimed_by_another_group_on_same_source(
    revision,
):
    s, h, body, orig = revision
    other = submit_return(s, body.model_copy(update={"id": uuid4()}), "fictional-competing-group")
    before = snapshot(s)
    with pytest.raises(IntegrityError) as caught, s["engine"].begin() as c:
        bad = h | dict(return_id=other.id, idempotency_key="fictional-takeover")
        c.execute(insert(Revision).values(**bad))
        row = member(bad, orig) | dict(return_id=other.id)
        c.execute(insert(Member).values(**row))
        c.execute(
            update(FinancialOperation)
            .where(FinancialOperation.id == row["operation_id"])
            .values(version=2, current_journal_id=row["journal_id"])
        )
        c.exec_driver_sql("SET CONSTRAINTS zz_tr_return_operations_complete IMMEDIATE")
    assert caught.value.orig.diag.constraint_name == "ck_return_revision_shape"
    assert snapshot(s) == before


def test_empty_ownership_migration_roundtrip_restores_triggers_and_protects_history(
    revision,
):
    s, header, _, orig = revision
    before = snapshot(s)
    m = return_ownership_migration()
    with s["engine"].begin() as c, Operations.context(MigrationContext.configure(c)):
        baseline = definitions(c)
        assert c.exec_driver_sql(
            "SELECT tgdeferrable,tginitdeferred,tgenabled FROM pg_trigger "
            "WHERE tgrelid='public.financial_operations'::regclass "
            "AND tgname='zz_tr_return_operations_complete'"
        ).one() == (True, True, "O")
        m["downgrade"]()
        m["upgrade"]()
        assert definitions(c) == baseline
        check(c, orig.postings[0].operation_id)
    with pytest.raises(IntegrityError) as rejected, s["engine"].begin() as c:
        c.execute(insert(Revision).values(**header))
        with Operations.context(MigrationContext.configure(c)):
            m["downgrade"]()
    assert rejected.value.orig.diag.constraint_name == "ck_return_revision_downgrade"
    assert snapshot(s) == before


def test_revision_born_v1_operation_resolves_its_real_owner_and_original_stays_readable(revision):
    s, _, _, original = revision
    before = snapshot(s)
    with s["engine"].connect() as c:
        born = stage(c, revision, new_operation=True)
        c.exec_driver_sql("SET CONSTRAINTS zz_tr_return_operations_complete IMMEDIATE")
        check(c, born["operation_id"])
        check(c, original.postings[0].operation_id)
        with pytest.raises(IntegrityError) as caught:
            check(c, original.postings[0].source_operation_id)
        assert caught.value.orig.diag.constraint_name == "ck_return_revision_shape"
        c.rollback()
    assert snapshot(s) == before
