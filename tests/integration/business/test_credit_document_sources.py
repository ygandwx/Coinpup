"""Pin reviewed source versions without opening the unfinished document commit gate."""

from types import SimpleNamespace
from uuid import uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from coinpup_api.business.credit_models import BusinessCredit
from coinpup_api.business.credit_revision_models import BusinessCreditRevision as CreditRevision
from coinpup_api.business.credit_schemas import BusinessCreditCreate
from sqlalchemy import insert, select, text
from sqlalchemy.exc import IntegrityError

from tests.integration.business.test_confirmed_document_revision_headers import cancelled
from tests.integration.business.test_confirmed_document_revision_headers import revision as revision
from tests.integration.business.test_credit_fact_validation import pending_fact, pending_origin
from tests.integration.business.test_credit_revision_storage import definitions
from tests.integration.business.test_credit_service import credit
from tests.integration.business.test_document_accrual_operation_heads import append_accrual
from tests.integration.business.test_draft_service import drafts as drafts
from tests.integration.conftest import credit_document_source_migration
from tests.integration.ledger.test_account_classes import snapshot
from tests.integration.ledger.test_posting_service_database import ledger_setup as ledger_setup

pytestmark = pytest.mark.integration


@pytest.fixture
def sources(revision):
    s, header, doc, _, origin = revision
    payload = BusinessCreditCreate(
        id=uuid4(),
        document_id=doc.id,
        expected_version=origin.document_version,
        credit_date=doc.issue_date,
        reason="Fictional original credit",
        lines=[
            dict(
                source_line_id=doc.lines[0].id,
                net_amount="0.01",
                tax_amount="0",
                recognition_date=doc.lines[0].recognition_date,
            )
        ],
    )
    original = credit(s, payload)
    credit_header = dict(
        id=uuid4(),
        ledger_id=s["ledger"],
        credit_id=original.id,
        document_id=doc.id,
        group_version=2,
        previous_revision_id=None,
        action="correct",
        revision_reason="Fictional reviewed credit source",
        credit_date=doc.issue_date,
        credit_reason="Fictional replacement",
        created_by=s["owner"],
        idempotency_key="fictional-credit",
    )
    return s, header, doc, origin, original, credit_header, payload


def facts(c, ledger, credits=None, journals=None):
    return (
        c.execute(
            text(
                "SELECT * FROM coinpup_credit_document_sources(CAST(:ledger AS uuid),"
                "CAST(:credits AS uuid[]),CAST(:journals AS uuid[])) ORDER BY document_version"
            ),
            dict(ledger=ledger, credits=credits, journals=journals),
        )
        .mappings()
        .all()
    )


def bound(c, model, identifier):
    return c.scalar(select(model.document_revision_id).where(model.id == identifier))


def end_credit(header, **changes):
    return header | dict(action="cancel", credit_date=None, credit_reason=None) | changes


def test_original_sources_are_scoped_and_survive_empty_migration_roundtrip(sources):
    s, _, doc, origin, original, _, payload = sources
    before = snapshot(s)
    with s["engine"].begin() as c, Operations.context(MigrationContext.configure(c)):
        (row,) = facts(c, s["ledger"], [original.id])
        assert (
            row["document_version"],
            row["source_operation_version"],
            row["asset_id"],
            row["source_journal_id"],
            row["description"],
        ) == (
            origin.document_version,
            1,
            doc.asset_id,
            origin.postings[0].journal_id,
            doc.lines[0].description,
        )
        assert facts(c, s["ledger"], journals=[original.postings[0].journal_id]) == [row]
        for ledger, ids, journals in (
            (uuid4(), [original.id], None),
            (s["ledger"], [], None),
            (s["ledger"], None, []),
            (s["ledger"], None, None),
            (s["ledger"], [uuid4()], None),
        ):
            assert facts(c, ledger, ids, journals) == []
        baseline = definitions(c)
        migration = credit_document_source_migration()
        migration["downgrade"]()
        migration["upgrade"]()
        assert definitions(c) == baseline
        assert facts(c, s["ledger"], [original.id]) == [row]
    assert credit(s, payload) == original and snapshot(s) == before


def test_new_origin_and_correction_pin_their_own_reviewed_source(sources):
    s, h, doc, origin, original, credit_header, payload = sources
    before = snapshot(s)
    with pytest.raises(IntegrityError) as failure, s["engine"].begin() as c:
        (old,) = facts(c, s["ledger"], [original.id])
        first = append_accrual(
            c,
            h,
            doc,
            origin.postings[0],
            "correct",
            2,
            fact_changes=dict(description="Fictional revised source"),
        )
        new_id = pending_origin(
            c, original, header_changes=dict(source_version=h["document_version"])
        )
        (new,) = facts(c, s["ledger"], [new_id])
        assert bound(c, BusinessCredit, new_id) == h["id"]
        assert bound(c, BusinessCredit, original.id) is None
        pending_fact(c, credit_header, original)
        assert bound(c, CreditRevision, credit_header["id"]) == h["id"]
        original_fact, replacement = facts(c, s["ledger"], [original.id])
        assert original_fact == old
        for row in (new, replacement):
            assert (
                row["document_version"],
                row["source_operation_version"],
                row["source_journal_id"],
                row["description"],
            ) == (h["document_version"], 2, first["journal_id"], "Fictional revised source")
        later = cancelled(h) | dict(
            id=uuid4(),
            previous_revision_id=h["id"],
            previous_version=h["document_version"],
            document_version=h["document_version"] + 1,
            idempotency_key="fictional-terminal-document",
        )
        append_accrual(c, later, doc, SimpleNamespace(**first), "cancel", 3)
        end = end_credit(
            credit_header,
            id=uuid4(),
            group_version=3,
            previous_revision_id=credit_header["id"],
            idempotency_key="fictional-terminal-credit",
        )
        c.execute(insert(CreditRevision), end)
        assert bound(c, CreditRevision, end["id"]) == h["id"]
        assert facts(c, s["ledger"], [original.id]) == [old, replacement]
        assert facts(c, s["ledger"], [new_id]) == [new]
        # No partial document revision is allowed to commit in this preparation package.
        c.exec_driver_sql("SET CONSTRAINTS tr_document_revisions_complete IMMEDIATE")
    assert failure.value.orig.diag.constraint_name == "ck_document_revision_complete"
    assert credit(s, payload) == original and snapshot(s) == before


def test_cancelling_original_credit_keeps_original_source_after_document_revision(sources):
    s, h, doc, origin, original, credit_header, _ = sources
    before = snapshot(s)
    with pytest.raises(IntegrityError) as failure, s["engine"].begin() as c:
        old = facts(c, s["ledger"], [original.id])
        append_accrual(c, h, doc, origin.postings[0], "correct", 2)
        c.execute(insert(CreditRevision), end_credit(credit_header))
        assert bound(c, CreditRevision, credit_header["id"]) is None
        assert facts(c, s["ledger"], [original.id]) == old
        c.exec_driver_sql("SET CONSTRAINTS tr_document_revisions_complete IMMEDIATE")
    assert failure.value.orig.diag.constraint_name == "ck_document_revision_complete"
    assert snapshot(s) == before


@pytest.mark.parametrize(
    "fault",
    [
        "forged-origin",
        "forged-correction",
        "old-version",
        "cancelled-origin",
        "cancelled-correction",
        "downgrade",
    ],
)
def test_invalid_or_discarded_source_binding_rolls_back_all_tables(sources, fault):
    s, h, doc, origin, original, credit_header, _ = sources
    before = snapshot(s)
    with pytest.raises(IntegrityError) as failure, s["engine"].begin() as c:
        first = append_accrual(c, h, doc, origin.postings[0], "correct", 2)
        if fault.startswith("cancelled"):
            later = cancelled(h) | dict(
                id=uuid4(),
                previous_revision_id=h["id"],
                previous_version=h["document_version"],
                document_version=h["document_version"] + 1,
                idempotency_key="fictional-terminal-document",
            )
            append_accrual(c, later, doc, SimpleNamespace(**first), "cancel", 3)
        if fault.endswith("correction"):
            changes = dict(document_revision_id=uuid4()) if fault.startswith("forged") else {}
            c.execute(insert(CreditRevision), credit_header | changes)
        else:
            changes = dict(source_version=h["document_version"])
            if fault == "old-version":
                changes["source_version"] = origin.document_version
            elif fault == "forged-origin":
                changes["document_revision_id"] = uuid4()
            pending_origin(c, original, header_changes=changes)
            if fault == "downgrade":
                with Operations.context(MigrationContext.configure(c)):
                    credit_document_source_migration()["downgrade"]()
    expected = (
        "ck_credit_document_source_downgrade"
        if fault == "downgrade"
        else "ck_credit_document_version"
    )
    assert failure.value.orig.diag.constraint_name == expected
    assert snapshot(s) == before
