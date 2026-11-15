"""Immutable original receipts and real historical journal legs for allocation returns."""

from datetime import date
from decimal import Decimal
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
from sqlalchemy import insert, text, update
from sqlalchemy.exc import IntegrityError

from tests.integration.business.test_allocation_return_revision_headers import revision as revision
from tests.integration.business.test_allocation_return_revision_members import member
from tests.integration.business.test_allocation_return_service import setup_return, submit_return
from tests.integration.business.test_allocation_return_storage import pending, rows
from tests.integration.business.test_credit_fact_validation import copy_posting, stored
from tests.integration.business.test_credit_revision_storage import definitions
from tests.integration.business.test_draft_service import drafts as drafts
from tests.integration.conftest import return_historical_migration
from tests.integration.ledger.test_account_classes import snapshot
from tests.integration.ledger.test_posting_service_database import ledger_setup as ledger_setup

pytestmark = pytest.mark.integration


def check_origin(c, identifier):
    c.execute(text("SELECT coinpup_validate_return_origin_facts(:id)"), {"id": identifier})


def check_posting(c, identifier, *, forged=False):
    common = "'{}'::jsonb" if forged else "coinpup_validate_journal_common(:id)"
    c.execute(
        text(f"SELECT coinpup_validate_return_posting_fact(:id,{common})"), {"id": identifier}
    )


@pytest.mark.parametrize("direction", ["receipt", "payout"])
@pytest.mark.parametrize("asset", ["USD", "EUR"])
def test_committed_origin_and_postings_remain_provable_after_later_returns(
    drafts, direction, asset
):
    s = drafts
    _, _, _, body = setup_return(s, direction=direction, asset=asset, count=2)
    first = submit_return(s, body)
    second = submit_return(s, body.model_copy(update={"id": uuid4()}), "fictional-another-return")
    before = snapshot(s)
    with s["engine"].connect() as c:
        c.exec_driver_sql("SET TRANSACTION READ ONLY")
        for group in (first, second):
            check_origin(c, group.id)
            for posting in group.postings:
                check_posting(c, posting.journal_id)
    assert submit_return(s, body) == first and snapshot(s) == before


def stage(c, setup, *, new_operation=False, member_changes=None, journal_changes=None, lines=None):
    s, h, _, original = setup
    acquire_write_lock(c)
    old = original.postings[0]
    row = member(h, original) | dict(
        payment_amount=Decimal(old.payment_amount), document_amount=Decimal(old.document_amount)
    )
    if new_operation:
        row.update(
            action="create",
            operation_id=uuid4(),
            operation_version=1,
            previous_line_no=None,
            previous_operation_version=None,
            previous_journal_id=None,
            reversal_journal_id=None,
            source_return_no=2,
            source_predecessor_operation_id=old.operation_id,
        )
        op = stored(c, FinancialOperation, old.operation_id) | dict(
            id=row["operation_id"], current_journal_id=row["journal_id"]
        )
        c.execute(insert(FinancialOperation).values(**op))
    else:
        c.execute(
            update(FinancialOperation)
            .where(FinancialOperation.id == old.operation_id)
            .values(version=2, current_journal_id=row["journal_id"])
        )
    c.execute(insert(Revision).values(**h))
    c.execute(insert(Member).values(**(row | (member_changes or {}))))
    copy_posting(
        c,
        old.journal_id,
        dict(
            id=row["journal_id"],
            operation_id=row["operation_id"],
            operation_version=row["operation_version"],
            transaction_date=h["return_date"],
            recognition_date=h["return_date"],
            description=h["return_reason"],
            revision_reason=None if new_operation else h["revision_reason"],
        )
        | (journal_changes or {}),
        line_changes=lines,
    )
    return row


@pytest.mark.parametrize("new_operation", [False, True])
def test_old_and_new_postings_use_their_own_journal_version_and_reason(revision, new_operation):
    s, _, body, orig = revision
    before = snapshot(s)
    with s["engine"].connect() as c:
        row = stage(c, revision, new_operation=new_operation)
        check_origin(c, orig.id)
        check_posting(c, orig.postings[0].journal_id)
        check_posting(c, row["journal_id"])
        with pytest.raises(IntegrityError) as rejected:
            c.exec_driver_sql("SET CONSTRAINTS tr_business_return_revisions_complete IMMEDIATE")
        assert rejected.value.orig.diag.constraint_name == "ck_return_revision_shape"
        c.rollback()
    assert submit_return(s, body) == orig and snapshot(s) == before


@pytest.mark.parametrize(
    "member_changes,journal_changes,lines",
    [
        ({"payment_amount": Decimal("19.99"), "document_amount": Decimal("19.99")}, None, None),
        (None, {"revision_reason": "Fictional other reason"}, None),
        (None, {"recognition_date": date(2026, 11, 9)}, None),
        (None, {"description": "Fictional different explanation"}, None),
        (None, None, {"party_id": None}),
        (None, None, {"document_id": None, "document_line_id": None, "project_id": None}),
    ],
)
def test_balanced_but_wrong_posting_is_rejected_and_all_tables_stay_unchanged(
    revision, member_changes, journal_changes, lines
):
    s, _, _, _ = revision
    before = snapshot(s)
    with pytest.raises(IntegrityError) as rejected, s["engine"].begin() as c:
        row = stage(
            c, revision, member_changes=member_changes, journal_changes=journal_changes, lines=lines
        )
        check_posting(c, row["journal_id"])
    assert rejected.value.orig.diag.constraint_name == "ck_business_allocation_return_shape"
    assert snapshot(s) == before


@pytest.mark.parametrize(
    "fault", ["missing-origin", "missing-posting", "forged-common", "ambiguous"]
)
def test_missing_ambiguous_or_forged_inputs_cannot_validate(revision, fault):
    s, h, _, orig = revision
    before = snapshot(s)
    old = orig.postings[0]
    with pytest.raises(IntegrityError) as rejected, s["engine"].begin() as c:
        if fault == "missing-origin":
            check_origin(c, uuid4())
        elif fault == "missing-posting":
            check_posting(c, uuid4())
        elif fault == "forged-common":
            check_posting(c, old.journal_id, forged=True)
        else:
            c.execute(insert(Revision).values(**h))
            row = member(h, orig) | dict(previous_journal_id=uuid4(), journal_id=old.journal_id)
            c.execute(insert(Member).values(**row))
            check_posting(c, old.journal_id)
    assert rejected.value.orig.diag.constraint_name == "ck_business_allocation_return_shape"
    assert snapshot(s) == before


def test_empty_historical_migration_roundtrip_and_downgrade_guard_preserve_origin(revision):
    s, h, _, orig = revision
    before = snapshot(s)
    m = return_historical_migration()
    with s["engine"].begin() as c, Operations.context(MigrationContext.configure(c)):
        latest = definitions(c)
        m["downgrade"]()
        assert (
            c.scalar(text("SELECT to_regprocedure('coinpup_validate_return_origin_facts(uuid)')"))
            is None
        )
        m["upgrade"]()
        assert definitions(c) == latest
        check_origin(c, orig.id)
    with pytest.raises(IntegrityError) as caught, s["engine"].begin() as c:
        c.execute(insert(Revision).values(**h))
        with Operations.context(MigrationContext.configure(c)):
            m["downgrade"]()
    assert caught.value.orig.diag.constraint_name == "ck_return_revision_downgrade"
    assert snapshot(s) == before


@pytest.mark.parametrize("fault", ["v1", "wrong-status", "wrong-anchor", "incomplete-json"])
def test_origin_rejects_well_formed_journals_with_a_different_creation_receipt(drafts, fault):
    s = drafts
    values = rows(s)
    before = snapshot(s)
    receipt_changes = {
        "v1": {"hash_version": 1},
        "wrong-status": {"response_status": 200},
        "wrong-anchor": {"operation_id": values["source_operation_id"]},
        "incomplete-json": {"response": {"id": str(values["group"]["id"])}},
    }[fault]
    with pytest.raises(IntegrityError) as rejected, s["engine"].begin() as c:
        pending(c, values, receipt_changes=receipt_changes)
        check_posting(c, values["journal"]["id"])
        check_origin(c, values["group"]["id"])
    assert rejected.value.orig.diag.constraint_name == "ck_business_allocation_return_shape"
    assert snapshot(s) == before
