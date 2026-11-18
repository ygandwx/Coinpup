"""A native complete proof retains real postings, all identities and permanent receipts."""

from types import SimpleNamespace
from uuid import uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from coinpup_api.business.allocation_return_revisions import BusinessAllocationReturnRevisionService
from coinpup_api.business.document_fixed_receipts import replay_document_revision
from coinpup_api.business.document_revision_models import BusinessDocumentRevision as Header
from coinpup_api.business.document_revision_schemas import (
    ConfirmedDocumentCancellation,
    ConfirmedDocumentCorrection,
)
from coinpup_api.ledger.models import CommandReceipt, FinancialOperation, Journal
from sqlalchemy import func, insert, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from tests.integration.business.test_confirmed_document_revision_headers import cancelled
from tests.integration.business.test_confirmed_document_revision_headers import revision as revision
from tests.integration.business.test_credit_document_origin import all_rows
from tests.integration.business.test_credit_document_sources import sources as sources
from tests.integration.business.test_credit_revision_storage import definitions
from tests.integration.business.test_document_accrual_operation_heads import append_accrual
from tests.integration.business.test_document_fixed_receipts import save
from tests.integration.business.test_document_revision_capacity import replacement
from tests.integration.business.test_document_revision_journals import confirmed
from tests.integration.business.test_document_revision_receipt_seals import receipt_values
from tests.integration.business.test_draft_service import drafts as drafts
from tests.integration.business.test_return_current_document_capacity import (
    change_document,
    full_return,
)
from tests.integration.business.test_return_revision_service import cancel
from tests.integration.conftest import document_group_migration
from tests.integration.ledger.test_account_classes import snapshot
from tests.integration.ledger.test_posting_service_database import ledger_setup as ledger_setup

pytestmark = pytest.mark.integration


def prove(c, h):
    c.execute(
        select(func.coinpup_validate_document_revision(h["ledger_id"], h["document_id"], h["id"]))
    )


@pytest.mark.parametrize("kind", ["invoice", "bill"])
@pytest.mark.parametrize("count", [1, 200])
def test_complete_replacement_then_cancel_preserves_all_real_postings_and_old_receipts(
    revision, kind, count
):
    s, template, *_ = revision
    doc, old = confirmed(s, kind, count, key="fictional-native-origin")
    h = template | dict(
        document_id=doc.id,
        confirmation_id=doc.confirmation_id,
        previous_version=doc.version,
        document_version=doc.version + 1,
        party_id=doc.party_id,
        party_snapshot=doc.party_snapshot,
        asset_id=doc.asset_id,
        issue_date=doc.issue_date,
        due_date=doc.due_date,
        notes=doc.notes,
        idempotency_key="fictional-native-correction",
    )
    before = snapshot(s)
    with s["engine"].connect() as c, c.begin() as tx, Session(c) as db:
        c.execute(insert(Header), h)
        new = []
        for position, prior in enumerate(old, 1):
            identifier = uuid4()
            row = append_accrual(
                c,
                h,
                doc,
                prior,
                "create",
                1,
                source_id=identifier,
                fact_changes=dict(line_no=position),
                header_saved=True,
            )
            new.append(row)
            append_accrual(
                c, h, doc, prior, "cancel", 2, source_id=prior.source_line_id, header_saved=True
            )
        proposed = replacement(doc)
        proposed = proposed.model_copy(
            update={
                "lines": [
                    item.model_copy(update={"id": row["source_line_id"]})
                    for item, row in zip(proposed.lines, new, strict=True)
                ]
            }
        )
        command = ConfirmedDocumentCorrection(
            expected_version=doc.version, reason=h["reason"], replacement=proposed
        )
        fixed, receipt, digest = save(c, db, h, "correct", command)
        prove(c, h)
        assert len(fixed.replacement.lines) == len(fixed.reversals) == count
        assert len({p.operation_id for p in old} | {r["operation_id"] for r in new}) == 2 * count
        terminal = cancelled(h) | dict(
            id=uuid4(),
            previous_revision_id=h["id"],
            previous_version=h["document_version"],
            document_version=h["document_version"] + 1,
            idempotency_key="fictional-native-cancel",
        )
        c.execute(insert(Header), terminal)
        for row in new:
            append_accrual(
                c,
                terminal,
                doc,
                SimpleNamespace(**row),
                "cancel",
                2,
                source_id=row["source_line_id"],
                header_saved=True,
            )
        ending = ConfirmedDocumentCancellation(
            expected_version=terminal["previous_version"], reason=terminal["reason"]
        )
        ended, last_receipt, last_digest = save(c, db, terminal, "cancel", ending)
        prove(c, terminal)
        prove(c, h)
        assert ended.replacement is None and ended.previous == fixed.replacement
        ids = [p.operation_id for p in old] + [r["operation_id"] for r in new]
        journals = (
            c.execute(select(Journal.sealed).where(Journal.operation_id.in_(ids))).scalars().all()
        )
        assert len(journals) == 4 * count and all(journals)
        frozen = all_rows(c)
        assert (
            replay_document_revision(db, receipt, s["ledger"], doc.id, "correct", command, digest)
            == fixed
        )
        assert (
            replay_document_revision(
                db, last_receipt, s["ledger"], doc.id, "cancel", ending, last_digest
            )
            == ended
        )
        assert all_rows(c) == frozen
        with pytest.raises(IntegrityError) as gated, c.begin_nested():
            c.exec_driver_sql("SET CONSTRAINTS tr_document_revisions_complete IMMEDIATE")
        assert gated.value.orig.diag.constraint_name == "ck_document_revision_complete"
        tx.rollback()
    assert snapshot(s) == before


@pytest.mark.parametrize(
    "fault", ["missing", "wrong-scope", "wrong-receipt", "no-head-update", "empty-postings"]
)
def test_incomplete_groups_fail_native_proof_and_roll_back(revision, fault):
    s, h, doc, _, origin = revision
    h = h | dict(idempotency_key="fictional-invalid-native-group")
    before = snapshot(s)
    with pytest.raises(IntegrityError) as failed, s["engine"].begin() as c, Session(c) as db:
        if fault == "missing":
            prove(c, h)
        else:
            # Missing lines are inserted as an intentionally incomplete native journal fixture.
            if fault == "empty-postings":
                from coinpup_api.business.document_revision_models import (
                    BusinessDocumentAccrual as Link,
                )
                from coinpup_api.business.document_revision_models import (
                    BusinessDocumentRevisionLine as Fact,
                )

                from tests.integration.business.test_document_accrual_links import accrual, post
                from tests.integration.business.test_document_revision_line_facts import line

                c.execute(insert(Header), h)
                f = line(h, doc)
                c.execute(insert(Fact), f)
                row = accrual(h, f, origin.postings[0], "correct")
                post(c, h, f, row)
                c.execute(insert(Link), row)
                c.execute(
                    update(FinancialOperation)
                    .where(FinancialOperation.id == origin.postings[0].operation_id)
                    .values(version=2, current_journal_id=row["journal_id"])
                )
            else:
                append_accrual(c, h, doc, origin.postings[0], "correct", 2, fault=fault)
            values = receipt_values(db, h, doc)
            if fault == "wrong-receipt":
                values["response"]["reason"] = "Fictional mismatched receipt"
            c.execute(insert(CommandReceipt), values)
            prove(c, h | dict(ledger_id=uuid4()) if fault == "wrong-scope" else h)
    expected = {
        "missing": "ck_document_revision_scope",
        "wrong-scope": "ck_document_revision_scope",
        "wrong-receipt": "ck_document_revision_receipt",
        "no-head-update": "ck_operation_history",
        "empty-postings": "ck_journal_reversal",
    }
    assert failed.value.orig.diag.constraint_name == expected[fault]
    assert snapshot(s) == before


def test_group_proof_empty_roundtrip_and_protected_history(revision):
    s, h, doc, _, origin = revision
    with s["engine"].begin() as c, Operations.context(MigrationContext.configure(c)):
        original = definitions(c)
        m = document_group_migration()
        m["downgrade"]()
        m["upgrade"]()
        assert definitions(c) == original
    before = snapshot(s)
    with pytest.raises(IntegrityError) as guarded, s["engine"].begin() as c:
        append_accrual(c, h, doc, origin.postings[0], "correct", 2)
        with Operations.context(MigrationContext.configure(c)):
            m["downgrade"]()
    assert guarded.value.orig.diag.constraint_name == "ck_document_group_downgrade"
    assert snapshot(s) == before


def test_cancel_reason_must_match_the_whole_document_even_without_a_replacement(revision):
    s, h, doc, _, origin = revision
    h = cancelled(h) | dict(idempotency_key="fictional-wrong-cancel-reason")
    before = snapshot(s)
    with pytest.raises(IntegrityError) as failed, s["engine"].begin() as c, Session(c) as db:
        append_accrual(c, h, doc, origin.postings[0], "cancel", 2, fault="wrong-cancel-reason")
        command = ConfirmedDocumentCancellation(
            expected_version=h["previous_version"], reason=h["reason"]
        )
        save(c, db, h, "cancel", command)
        prove(c, h)
    assert failed.value.orig.diag.constraint_name == "ck_document_accrual_journals"
    assert snapshot(s) == before


def test_complete_postings_and_receipt_cannot_hide_overdrawn_effective_dependencies(sources):
    s, h, doc, confirmed, _, _, _ = sources
    h = h | dict(idempotency_key="fictional-complete-but-overdrawn")
    _, _, returned = full_return(s, doc)
    BusinessAllocationReturnRevisionService(s["engine"]).cancel(
        s["owner"], s["ledger"], returned.id, cancel(1), "fictional-restore-application"
    )
    before = snapshot(s)
    with pytest.raises(IntegrityError) as failed, s["engine"].begin() as c, Session(c) as db:
        change_document(c, h, doc, confirmed.postings[0], "at-limit")
        proposed = replacement(
            doc,
            {
                doc.lines[0].id: dict(
                    quantity="1", unit_price="10", discount_amount="0", tax_rate_percent="0"
                )
            },
        )
        command = ConfirmedDocumentCorrection(
            expected_version=h["previous_version"], reason=h["reason"], replacement=proposed
        )
        save(c, db, h, "correct", command)
        prove(c, h)
    assert failed.value.orig.diag.constraint_name == "ck_document_dependency_capacity"
    assert snapshot(s) == before
