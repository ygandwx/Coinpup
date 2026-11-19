"""Complete fictional whole-document groups really commit; partial groups never do."""

from uuid import uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from coinpup_api.business.document_fixed_receipts import replay_document_revision
from coinpup_api.business.document_revision_models import BusinessDocumentRevision as Header
from coinpup_api.business.document_revision_schemas import (
    ConfirmedDocumentCancellation,
    ConfirmedDocumentCorrection,
)
from coinpup_api.ledger.models import CommandReceipt, Journal
from sqlalchemy import delete, insert, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from tests.integration.business.test_confirmed_document_revision_headers import cancelled
from tests.integration.business.test_confirmed_document_revision_headers import revision as revision
from tests.integration.business.test_credit_document_origin import all_rows
from tests.integration.business.test_credit_revision_storage import definitions
from tests.integration.business.test_document_accrual_operation_heads import append_accrual
from tests.integration.business.test_document_fixed_receipts import save
from tests.integration.business.test_document_revision_capacity import replacement
from tests.integration.business.test_document_revision_journals import confirmed
from tests.integration.business.test_draft_service import drafts as drafts
from tests.integration.conftest import document_lifecycle_migration
from tests.integration.ledger.test_account_classes import snapshot
from tests.integration.ledger.test_posting_service_database import ledger_setup as ledger_setup

pytestmark = pytest.mark.integration


def header(template, doc):
    return template | dict(
        id=uuid4(),
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
        idempotency_key="fictional-committed-correction",
    )


def correction(c, db, h, doc, postings, *, fault=None):
    c.execute(insert(Header), h)
    rows = []
    for position, prior in enumerate(postings, 1):
        rows.append(
            append_accrual(
                c,
                h,
                doc,
                prior,
                "correct",
                2,
                source_id=prior.source_line_id,
                fact_changes=dict(line_no=position),
                fault=fault,
                header_saved=True,
            )
        )
    command = ConfirmedDocumentCorrection(
        expected_version=h["previous_version"], reason=h["reason"], replacement=replacement(doc)
    )
    saved = None if fault == "no-receipt" else save(c, db, h, "correct", command)
    return command, saved, rows


@pytest.mark.parametrize("kind", ["invoice", "bill"])
@pytest.mark.parametrize("count", [1, 200])
def test_correct_and_cancel_commit_separately_then_replay_both_permanent_versions(
    revision, kind, count
):
    from types import SimpleNamespace

    s, template, *_ = revision
    doc, postings = confirmed(s, kind, count, key="fictional-committed-origin")
    h = header(template, doc)
    with s["engine"].begin() as c, Session(c) as db:
        command, (fixed, _, digest), rows = correction(c, db, h, doc, postings)
        assert len(fixed.reversals) == len(fixed.replacement.lines) == count
        # No direct proof call: the real deferred constraint must prove the transaction.
    with s["engine"].connect() as c:
        assert (
            c.scalar(select(Header.document_version).where(Header.id == h["id"])) == doc.version + 1
        )
        assert all(
            c.scalars(
                select(Journal.sealed).where(
                    Journal.operation_id.in_([p.operation_id for p in postings])
                )
            )
        )
    terminal = cancelled(h) | dict(
        id=uuid4(),
        previous_revision_id=h["id"],
        previous_version=h["document_version"],
        document_version=h["document_version"] + 1,
        idempotency_key="fictional-committed-cancel",
    )
    ending = ConfirmedDocumentCancellation(
        expected_version=terminal["previous_version"], reason=terminal["reason"]
    )
    with s["engine"].begin() as c, Session(c) as db:
        c.execute(insert(Header), terminal)
        for row in rows:
            append_accrual(
                c,
                terminal,
                doc,
                SimpleNamespace(**row),
                "cancel",
                3,
                source_id=row["source_line_id"],
                header_saved=True,
            )
        ended, _, last_digest = save(c, db, terminal, "cancel", ending)
        assert ended.replacement is None and ended.previous == fixed.replacement
    with s["engine"].begin() as c, Session(c) as db:
        frozen = all_rows(c)
        for action, request, expected, key, value in (
            ("correct", command, fixed, h["idempotency_key"], digest),
            ("cancel", ending, ended, terminal["idempotency_key"], last_digest),
        ):
            receipt = db.get(CommandReceipt, (s["ledger"], key))
            assert (
                replay_document_revision(db, receipt, s["ledger"], doc.id, action, request, value)
                == expected
            )
        assert all_rows(c) == frozen
        seals = c.scalars(
            select(Journal.sealed).where(
                Journal.operation_id.in_([p.operation_id for p in postings])
            )
        ).all()
        assert len(seals) == 4 * count and all(seals)
    before = snapshot(s)
    # History and the original terminal operation cannot be revived or rewritten.
    for statement in (
        update(Header).where(Header.id == h["id"]).values(reason="Fictional edit"),
        delete(Header).where(Header.id == terminal["id"]),
        insert(Header).values(
            **(
                h
                | dict(
                    id=uuid4(),
                    previous_revision_id=terminal["id"],
                    previous_version=terminal["document_version"],
                    document_version=terminal["document_version"] + 1,
                    idempotency_key="fictional-after-terminal",
                )
            )
        ),
    ):
        with pytest.raises(IntegrityError), s["engine"].begin() as c:
            c.execute(statement)
        assert snapshot(s) == before
    with pytest.raises(IntegrityError) as refused, s["engine"].begin() as c:
        with Operations.context(MigrationContext.configure(c)):
            document_lifecycle_migration()["downgrade"]()
    assert refused.value.orig.diag.constraint_name == "ck_document_lifecycle_downgrade"
    assert snapshot(s) == before


@pytest.mark.parametrize("fault", ["no-receipt", "no-head-update", "wrong-cancel-reason"])
def test_commit_rejects_partial_group_and_preserves_every_table(revision, fault):
    s, template, doc, _, original = revision
    h = template | dict(id=uuid4(), idempotency_key="fictional-partial-group")
    # Original 201 uses posting.line_id; native helpers use source_line_id.
    from types import SimpleNamespace

    postings = [
        SimpleNamespace(**p.model_dump(), source_line_id=p.line_id) for p in original.postings
    ]
    before = snapshot(s)
    with pytest.raises(IntegrityError) as failed, s["engine"].begin() as c, Session(c) as db:
        correction(c, db, h, doc, postings, fault=fault)
    expected = {
        "no-receipt": {"fk_business_document_revision_receipt", "ck_document_revision_receipt"},
        "no-head-update": {"ck_operation_history"},
        "wrong-cancel-reason": {"ck_document_accrual_journals"},
    }
    assert failed.value.orig.diag.constraint_name in expected[fault]
    assert snapshot(s) == before


def test_empty_roundtrip_restores_the_entire_function_catalog(revision):
    s, *_ = revision
    with s["engine"].begin() as c, Operations.context(MigrationContext.configure(c)):
        before = definitions(c)
        migration = document_lifecycle_migration()
        migration["downgrade"]()
        migration["upgrade"]()
        assert definitions(c) == before
