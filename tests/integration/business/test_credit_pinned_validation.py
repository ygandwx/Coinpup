"""Historical credit journals prove their own source even after later document transitions."""

from types import SimpleNamespace
from uuid import uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from coinpup_api.business.credit_models import BusinessCreditLine
from coinpup_api.ledger.models import FinancialOperation
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from tests.integration.business.test_confirmed_document_revision_headers import cancelled
from tests.integration.business.test_confirmed_document_revision_headers import revision as revision
from tests.integration.business.test_credit_document_sources import sources as sources
from tests.integration.business.test_credit_fact_validation import (
    leaf,
    pending_fact,
    pending_origin,
)
from tests.integration.business.test_credit_service import credit
from tests.integration.business.test_document_accrual_operation_heads import append_accrual
from tests.integration.business.test_draft_service import drafts as drafts
from tests.integration.conftest import credit_document_validation_migration
from tests.integration.ledger.test_account_classes import snapshot
from tests.integration.ledger.test_posting_service_database import ledger_setup as ledger_setup

pytestmark = pytest.mark.integration


def terminal(h):
    return cancelled(h) | dict(
        id=uuid4(),
        previous_revision_id=h["id"],
        previous_version=h["document_version"],
        document_version=h["document_version"] + 1,
        idempotency_key="fictional-end-source",
    )


def test_original_new_and_revised_credit_leaves_keep_their_source_after_cancellation(sources):
    s, h, doc, origin, original, credit_header, payload = sources
    before = snapshot(s)
    with pytest.raises(IntegrityError) as failure, s["engine"].begin() as c:
        first = append_accrual(
            c,
            h,
            doc,
            origin.postings[0],
            "correct",
            2,
            fact_changes=dict(
                quantity="1",
                unit_price="0.03",
                discount_amount="0",
                tax_rate_percent="0",
                net_amount="0.03",
                tax_amount="0",
                total_amount="0.03",
            ),
        )
        new_id = pending_origin(
            c, original, header_changes=dict(source_version=h["document_version"])
        )
        new_journal = c.scalar(
            select(BusinessCreditLine.journal_id).where(BusinessCreditLine.credit_id == new_id)
        )
        revised = pending_fact(c, credit_header, original)
        journals = (original.postings[0].journal_id, new_journal, revised["journal_id"])
        for journal in journals:
            leaf(c, journal)
        append_accrual(c, terminal(h), doc, SimpleNamespace(**first), "cancel", 3)
        for journal in journals:
            leaf(c, journal)
        c.exec_driver_sql("SET CONSTRAINTS tr_document_revisions_complete IMMEDIATE")
    assert failure.value.orig.diag.constraint_name == "ck_document_revision_complete"
    assert credit(s, payload) == original and snapshot(s) == before


@pytest.mark.parametrize("correct_dimensions", [False, True])
def test_new_credit_facts_must_use_reviewed_replacement_dimensions(sources, correct_dimensions):
    s, h, doc, origin, original, credit_header, _ = sources
    before = snapshot(s)
    with pytest.raises(IntegrityError) as failure, s["engine"].begin() as c:
        append_accrual(
            c,
            h,
            doc,
            origin.postings[0],
            "correct",
            2,
            fact_changes=dict(project_id=None, project_snapshot=None),
        )
        # The original credit still references the old project; it remains a valid history.
        leaf(c, original.postings[0].journal_id)
        revised = pending_fact(
            c,
            credit_header,
            original,
            line_changes={"project_id": None} if correct_dimensions else None,
        )
        leaf(c, revised["journal_id"])
        c.exec_driver_sql("SET CONSTRAINTS tr_document_revisions_complete IMMEDIATE")
    expected = "ck_document_revision_complete" if correct_dimensions else "ck_business_credit_shape"
    assert failure.value.orig.diag.constraint_name == expected and snapshot(s) == before


@pytest.mark.parametrize("fault", ["bare-advance", "bare-cancel", "wrong-head", "downgrade"])
def test_history_proof_does_not_authorize_unregistered_source_heads(sources, fault):
    s, h, doc, origin, original, _, _ = sources
    before = snapshot(s)
    with pytest.raises(IntegrityError) as failure, s["engine"].begin() as c:
        if fault == "downgrade":
            append_accrual(c, h, doc, origin.postings[0], "correct", 2)
            with Operations.context(MigrationContext.configure(c)):
                credit_document_validation_migration()["downgrade"]()
        else:
            changed = (
                {"version": 2, "current_journal_id": original.postings[0].journal_id}
                if fault == "bare-advance"
                else (
                    {"version": 2, "status": "cancelled"}
                    if fault == "bare-cancel"
                    else {"current_journal_id": original.postings[0].journal_id}
                )
            )
            c.execute(
                update(FinancialOperation)
                .where(FinancialOperation.id == origin.postings[0].operation_id)
                .values(**changed)
            )
            leaf(c, original.postings[0].journal_id)
    expected = {
        "bare-advance": "ck_document_accrual_head",
        "bare-cancel": "ck_document_accrual_head",
        "wrong-head": "ck_posting_immutable",
        "downgrade": "ck_credit_document_validation_downgrade",
    }[fault]
    assert failure.value.orig.diag.constraint_name == expected and snapshot(s) == before
