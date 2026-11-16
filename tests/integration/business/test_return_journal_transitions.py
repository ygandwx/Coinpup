"""Exercise complete return transitions independently of the still-closed commit gate."""

import runpy
from datetime import date
from pathlib import Path
from uuid import uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from coinpup_api.business.allocation_return_revision_models import (
    BusinessAllocationReturnRevision as Revision,
)
from coinpup_api.ledger.models import FinancialOperation
from sqlalchemy import insert, text, update
from sqlalchemy.exc import IntegrityError

from tests.integration.business.test_allocation_return_revision_headers import revision as revision
from tests.integration.business.test_allocation_return_revision_receipts import (
    create_group,
    store_receipt,
)
from tests.integration.business.test_allocation_return_service import setup_return, submit_return
from tests.integration.business.test_credit_fact_validation import copy_posting
from tests.integration.business.test_credit_revision_storage import definitions
from tests.integration.business.test_draft_service import drafts as drafts
from tests.integration.ledger.test_account_classes import snapshot
from tests.integration.ledger.test_posting_service_database import ledger_setup as ledger_setup

pytestmark = pytest.mark.integration


def check_transition(c, revision_id):
    c.execute(text("SELECT coinpup_validate_return_transition(:id)"), {"id": revision_id})


def stage_transition(c, setup, *, cancel=False, asset="USD", fault=None):
    h, receipt = create_group(c, setup, cancel=cancel, asset=asset)
    if fault != "receipt":
        store_receipt(c, h, receipt)
    for position, member in enumerate(receipt["members"]):
        defect = fault if position == 0 else None
        old = member["previous"]["journal_id"]
        reversal = dict(
            id=member["reversal_journal_id"],
            operation_version=member["operation_version"],
            journal_kind="reversal",
            reverses_journal_id=old,
            revision_reason=h["revision_reason"],
        )
        if defect == "reason":
            reversal["revision_reason"] = "Fictional unrelated reversal reason"
        if defect == "date":
            reversal["recognition_date"] = date(2026, 11, 9)
        if defect != "missing":
            copy_posting(
                c,
                old,
                reversal,
                reverse=defect != "sign",
                line_changes={"party_id": None} if defect == "dimensions" else None,
            )
        current_journal = old
        if not cancel:
            current_journal = member["replacement"]["journal_id"]
            copy_posting(
                c,
                old,
                dict(
                    id=current_journal,
                    operation_version=member["operation_version"],
                    transaction_date=h["return_date"],
                    recognition_date=h["return_date"],
                    description=h["return_reason"] if defect != "posting" else "Fictional other",
                    revision_reason=h["revision_reason"],
                ),
            )
        if defect != "owner":
            c.execute(
                update(FinancialOperation)
                .where(FinancialOperation.id == member["operation_id"])
                .values(
                    version=member["operation_version"],
                    current_journal_id=current_journal,
                    status="cancelled" if cancel else "active",
                )
            )
    return h


@pytest.fixture
def group(drafts, request):
    asset, direction = getattr(request, "param", ("USD", "payout"))
    _, _, _, body = setup_return(drafts, count=2, direction=direction, asset=asset)
    lines = [
        line.model_copy(
            update={"payment_amount": "11" if asset == "EUR" else "10", "document_amount": "10"}
        )
        for line in body.lines
    ]
    body = body.model_copy(update={"lines": lines})
    return (drafts, body, submit_return(drafts, body)), asset


@pytest.mark.parametrize("group", [("USD", "receipt"), ("EUR", "payout")], indirect=True)
@pytest.mark.parametrize("cancel", [False, True])
def test_whole_revision_matches_original_sealed_legs_and_keeps_original_receipt(group, cancel):
    setup, asset = group
    s, body, original = setup
    before = snapshot(s)
    with s["engine"].connect() as c:
        h = stage_transition(c, setup, asset=asset, cancel=cancel)
        check_transition(c, h["id"])
        c.exec_driver_sql("SET CONSTRAINTS tr_journals_reversal_dimensions IMMEDIATE")
        c.rollback()
    assert submit_return(s, body) == original and snapshot(s) == before


@pytest.mark.parametrize(
    "fault,constraint",
    [
        ("receipt", "ck_return_revision_shape"),
        ("missing", "ck_return_revision_shape"),
        ("reason", "ck_return_revision_shape"),
        ("date", "ck_journal_reversal"),
        ("sign", "ck_journal_reversal"),
        ("posting", "ck_business_allocation_return_shape"),
        ("owner", "ck_return_revision_shape"),
        ("dimensions", "ck_journal_reversal_dimensions"),
    ],
)
def test_each_member_requires_exact_reversal_replacement_and_final_owner(group, fault, constraint):
    setup, asset = group
    s, _, _ = setup
    before = snapshot(s)
    with pytest.raises(IntegrityError) as error, s["engine"].begin() as c:
        h = stage_transition(c, setup, asset=asset, fault=fault)
        check_transition(c, h["id"])
        c.exec_driver_sql("SET CONSTRAINTS tr_journals_reversal_dimensions IMMEDIATE")
    assert error.value.orig.diag.constraint_name == constraint
    assert snapshot(s) == before


def test_transition_migration_round_trip_preserves_closed_gate_and_protected_history(revision):
    s, h, _, _ = revision
    migration = runpy.run_path(
        str(
            Path(__file__).resolve().parents[3]
            / "services/api/migrations/versions/20261109_0072_return_journal_transitions.py"
        )
    )
    before = snapshot(s)
    with s["engine"].begin() as c, Operations.context(MigrationContext.configure(c)):
        baseline = definitions(c)
        migration["downgrade"]()
        assert (
            c.scalar(text("SELECT to_regprocedure('coinpup_validate_return_transition(uuid)')"))
            is None
        )
        migration["upgrade"]()
        assert definitions(c) == baseline
    with pytest.raises(IntegrityError) as rejected, s["engine"].begin() as c:
        c.execute(insert(Revision).values(**h))
        with Operations.context(MigrationContext.configure(c)):
            migration["downgrade"]()
    assert rejected.value.orig.diag.constraint_name == "ck_return_revision_downgrade"
    with pytest.raises(IntegrityError) as missing, s["engine"].begin() as c:
        check_transition(c, uuid4())
    assert missing.value.orig.diag.constraint_name == "ck_return_revision_shape"
    assert snapshot(s) == before
