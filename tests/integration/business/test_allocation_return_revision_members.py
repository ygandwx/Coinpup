"""Scope, permanently occupied source slots and exact prefix facts are enforced in PostgreSQL."""

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
from coinpup_api.sync.models import ChangeLog
from sqlalchemy import delete, insert, select, update
from sqlalchemy.exc import IntegrityError

from tests.integration.business.test_allocation_return_revision_headers import revision as revision
from tests.integration.business.test_allocation_return_service import submit_return
from tests.integration.business.test_credit_revision_storage import definitions
from tests.integration.business.test_draft_service import drafts as drafts
from tests.integration.conftest import return_revision_member_migration
from tests.integration.ledger.test_account_classes import snapshot
from tests.integration.ledger.test_posting_service_database import ledger_setup as ledger_setup

pytestmark = pytest.mark.integration


def member(header, original):
    old = original.postings[0]
    return dict(
        id=uuid4(),
        revision_id=header["id"],
        return_id=original.id,
        payment_id=original.payment_id,
        ledger_id=original.ledger_id,
        source_allocation_line_id=old.source_allocation_line_id,
        source_operation_id=old.source_operation_id,
        member_no=1,
        action="correct",
        operation_id=old.operation_id,
        operation_version=2,
        previous_line_no=1,
        previous_operation_version=1,
        previous_journal_id=old.journal_id,
        reversal_journal_id=uuid4(),
        line_no=1,
        journal_id=uuid4(),
        source_version=1,
        source_return_no=1,
        source_predecessor_operation_id=None,
        prefix_payment_amount=Decimal(0),
        prefix_document_amount=Decimal(0),
        payment_amount=Decimal("12.34"),
        document_amount=Decimal("12.34"),
        absorbs_remainder=False,
    )


@pytest.mark.parametrize(
    "fault,expected",
    [
        ("source_id", "fk_return_revision_member_source"),
        ("source_operation", "fk_return_revision_member_source"),
        ("foreign_payment", "fk_return_revision_member_group"),
        ("group", "fk_return_revision_member_group"),
        ("slot_zero", "ck_return_revision_member_source_order"),
        ("source_v2", "ck_return_revision_member_source_order"),
        ("slot_gap", "ck_return_revision_member_source_predecessor"),
        ("negative", "ck_return_revision_member_prefix"),
        ("first_with_prefix", "ck_return_revision_member_prefix"),
        ("single_side", "ck_return_revision_member_prefix"),
        ("original_predecessor", "ck_return_revision_member_source_predecessor"),
        ("self_predecessor", "ck_return_revision_member_source_predecessor"),
        ("zero_cash", "ck_return_revision_member_replacement"),
        ("zero_document", "ck_return_revision_member_replacement"),
        ("missing_final", "ck_return_revision_member_replacement"),
        ("journal_reuse", "ck_return_revision_member_replacement"),
        ("advance_twice", "ck_return_revision_member_previous"),
        ("member_401", "ck_return_revision_member_position"),
    ],
)
def test_no_partial_or_wrongly_scoped_member_can_persist(revision, fault, expected):
    s, header, _, original = revision
    row = member(header, original)
    changes = {
        "source_id": {"source_allocation_line_id": uuid4()},
        "source_operation": {"source_operation_id": uuid4()},
        "foreign_payment": {"payment_id": uuid4()},
        "group": {"return_id": uuid4()},
        "slot_zero": {"source_return_no": 0, "source_predecessor_operation_id": None},
        "source_v2": {"source_version": 2},
        "slot_gap": {"source_return_no": 2},
        "negative": {"prefix_payment_amount": Decimal("-1")},
        "first_with_prefix": {"prefix_payment_amount": 1, "prefix_document_amount": 1},
        "single_side": {"prefix_payment_amount": 1},
        "original_predecessor": {
            "source_return_no": 2,
            "source_predecessor_operation_id": row["source_operation_id"],
        },
        "self_predecessor": {
            "source_return_no": 2,
            "source_predecessor_operation_id": row["operation_id"],
        },
        "zero_cash": {"payment_amount": Decimal(0)},
        "zero_document": {"document_amount": Decimal(0)},
        "missing_final": {"absorbs_remainder": None},
        "journal_reuse": {"journal_id": row["previous_journal_id"]},
        "advance_twice": {"operation_version": 3},
        "member_401": {"member_no": 401},
    }
    before = snapshot(s)
    with pytest.raises(IntegrityError) as caught, s["engine"].begin() as c:
        c.execute(insert(Revision).values(**header))
        c.execute(insert(Member).values(**(row | changes[fault])))
    assert caught.value.orig.diag.constraint_name == expected and snapshot(s) == before


@pytest.mark.parametrize("action", ["create", "correct", "cancel"])
def test_complete_transition_rows_alone_cannot_open_the_commit_gate(revision, action):
    s, header, _, original = revision
    row = member(header, original) | {"action": action}
    if action == "cancel":
        row.update(
            line_no=None,
            journal_id=None,
            payment_amount=None,
            document_amount=None,
            absorbs_remainder=None,
        )
    if action == "create":
        row.update(
            previous_line_no=None,
            previous_operation_version=None,
            previous_journal_id=None,
            reversal_journal_id=None,
            operation_version=1,
        )
    before = snapshot(s)
    with pytest.raises(IntegrityError) as caught, s["engine"].begin() as c:
        c.execute(insert(Revision).values(**header))
        c.execute(insert(Member).values(**row))
        assert (
            c.scalar(select(ChangeLog.entity_type).where(ChangeLog.entity_id == str(row["id"])))
            == "business_return_revision_members"
        )
        c.exec_driver_sql("SET CONSTRAINTS tr_return_revision_members_complete IMMEDIATE")
    assert (
        caught.value.orig.diag.constraint_name == "ck_return_revision_shape"
        and snapshot(s) == before
    )


@pytest.mark.parametrize("act", ["edit", "delete", "downgrade"])
def test_persisted_prefix_and_reserved_ordinal_cannot_be_rewritten(revision, act):
    s, header, _, original = revision
    before = snapshot(s)
    with pytest.raises(IntegrityError) as caught, s["engine"].begin() as c:
        c.execute(insert(Revision).values(**header))
        c.execute(insert(Member).values(**member(header, original)))
        if act == "edit":
            c.execute(update(Member).values(source_return_no=2))
        if act == "delete":
            c.execute(delete(Member))
        if act == "downgrade":
            with Operations.context(MigrationContext.configure(c)):
                return_revision_member_migration()["downgrade"]()
    assert caught.value.orig.diag.constraint_name == (
        "ck_return_revision_downgrade" if act == "downgrade" else "ck_return_revision_immutable"
    )
    assert snapshot(s) == before


def test_empty_member_roundtrip_restores_schema_and_keeps_original_201(revision):
    s, _, body, original = revision
    m = return_revision_member_migration()
    before = snapshot(s)
    with s["engine"].begin() as c, Operations.context(MigrationContext.configure(c)):
        baseline = definitions(c)
        m["downgrade"]()
        assert (
            c.exec_driver_sql(
                "SELECT to_regclass('public.business_return_revision_members')"
            ).scalar()
            is None
        )
        m["upgrade"]()
        assert definitions(c) == baseline
    assert submit_return(s, body) == original and snapshot(s) == before
