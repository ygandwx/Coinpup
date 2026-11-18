"""Prove real ledger heads and sealed journals without opening incomplete document groups."""

from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from coinpup_api.business.document_revision_models import BusinessDocumentAccrual as Link
from coinpup_api.business.document_revision_models import BusinessDocumentRevision as Header
from coinpup_api.business.document_revision_models import BusinessDocumentRevisionLine as Fact
from coinpup_api.ledger.models import FinancialOperation, Journal, JournalLine
from sqlalchemy import func, insert, select, update
from sqlalchemy.exc import IntegrityError

from tests.integration.business.test_confirmed_document_revision_headers import cancelled
from tests.integration.business.test_confirmed_document_revision_headers import revision as revision
from tests.integration.business.test_document_accrual_links import accrual, post
from tests.integration.business.test_document_revision_line_facts import line
from tests.integration.business.test_draft_service import drafts as drafts
from tests.integration.conftest import document_accrual_head_migration
from tests.integration.ledger.test_account_classes import snapshot
from tests.integration.ledger.test_posting_service_database import ledger_setup as ledger_setup

pytestmark = pytest.mark.integration


def append_accrual(
    c,
    header,
    doc,
    prior,
    action,
    version,
    *,
    source_id=None,
    fact_changes=None,
    fault=None,
    header_saved=False,
):
    if not header_saved:
        c.execute(insert(Header), header)
    fact = line(header, doc) | (fact_changes or {})
    if source_id is not None:
        fact["source_line_id"] = source_id
    if action != "cancel":
        c.execute(insert(Fact), fact)
    row = accrual(header, fact, prior, action) | dict(operation_version=version)
    journal_header = (
        header | dict(reason="Fictional different journal reason")
        if fault == "wrong-cancel-reason"
        else header
    )
    post(c, journal_header, fact, row, kind=doc.document_kind)
    originals = (
        c.execute(select(JournalLine.__table__).where(JournalLine.journal_id == prior.journal_id))
        .mappings()
        .all()
    )
    for journal, sign in ((row["reversal_journal_id"], -1), (row["journal_id"], 1)):
        if journal:
            c.execute(
                insert(JournalLine),
                [
                    dict(r)
                    | dict(
                        id=uuid4(),
                        journal_id=journal,
                        amount=-r["amount"]
                        if sign == -1
                        else Decimal(fact["total_amount"]).copy_sign(r["amount"]),
                        project_id=r["project_id"] if sign == -1 else fact["project_id"],
                        asset_id=r["asset_id"] if sign == -1 else header["asset_id"],
                        party_id=r["party_id"] if sign == -1 else header["party_id"],
                        document_line_id=fact["source_line_id"],
                    )
                    for r in originals
                ],
            )
    if fault != "unregistered":
        c.execute(insert(Link), row)
    if action != "create" and fault != "no-head-update":
        status = "cancelled" if action == "cancel" else "active"
        pointer = row["journal_id"] or prior.journal_id
        if fault == "wrong-pointer":
            status, pointer = "active", row["reversal_journal_id"]
        c.execute(
            update(FinancialOperation)
            .where(FinancialOperation.id == prior.operation_id)
            .values(version=version, status=status, current_journal_id=pointer)
        )
    return row


@pytest.mark.parametrize("initial", ["correct", "create"])
def test_each_origin_and_replacement_stays_valid_after_a_terminal_reversal(revision, initial):
    s, h, doc, _, origin = revision
    old = origin.postings[0]
    before = snapshot(s)
    with pytest.raises(IntegrityError) as err, s["engine"].begin() as c:
        first_version = 2 if initial == "correct" else 1
        source = old.line_id if initial == "correct" else uuid4()
        first = append_accrual(c, h, doc, old, initial, first_version, source_id=source)
        op = first["operation_id"]
        c.execute(select(func.coinpup_validate_operation_history(op)))
        assert c.scalar(select(Journal.sealed).where(Journal.id == first["journal_id"]))
        terminal = cancelled(h) | dict(
            id=uuid4(),
            previous_revision_id=h["id"],
            previous_version=h["document_version"],
            document_version=h["document_version"] + 1,
            idempotency_key="fictional-end-accrual",
        )
        current = SimpleNamespace(operation_id=op, journal_id=first["journal_id"])
        end = append_accrual(
            c, terminal, doc, current, "cancel", first_version + 1, source_id=source
        )
        c.exec_driver_sql(
            "SET CONSTRAINTS tr_document_accrual_journals,tr_document_accrual_facts,"
            "tr_document_accrual_head,tr_journals_reversal_dimensions IMMEDIATE"
        )
        c.execute(select(func.coinpup_validate_operation_history(op)))
        c.execute(select(func.coinpup_validate_business_confirmation(origin.id)))
        assert set(
            c.scalars(
                select(Journal.id).where(Journal.operation_id == op, Journal.sealed.is_(True))
            )
        ) == {
            key
            for key in (
                old.journal_id if initial == "correct" else None,
                first["reversal_journal_id"],
                first["journal_id"],
                end["reversal_journal_id"],
            )
            if key
        }
        actual = c.execute(
            select(
                FinancialOperation.version,
                FinancialOperation.status,
                FinancialOperation.current_journal_id,
            ).where(FinancialOperation.id == op)
        ).one()
        assert actual == (first_version + 1, "cancelled", first["journal_id"])
        c.exec_driver_sql("SET CONSTRAINTS tr_document_revisions_complete IMMEDIATE")
    assert err.value.orig.diag.constraint_name == "ck_document_revision_complete"
    assert snapshot(s) == before


@pytest.mark.parametrize(
    "fault,constraint",
    [
        ("unregistered", "ck_business_accrual_shape"),
        ("no-head-update", "ck_document_accrual_head"),
        ("wrong-pointer", "ck_document_accrual_head"),
        ("wrong-kind", "ck_business_accrual_shape"),
        ("downgrade", "ck_document_accrual_head_downgrade"),
    ],
)
def test_direct_or_mismatched_child_advance_rolls_back_every_row(revision, fault, constraint):
    s, h, doc, _, origin = revision
    prior = origin.postings[0]
    before = snapshot(s)
    with pytest.raises(IntegrityError) as err, s["engine"].begin() as c:
        row = append_accrual(c, h, doc, prior, "correct", 2, fault=fault)
        if fault == "unregistered":
            c.execute(select(func.coinpup_validate_operation_history(prior.operation_id)))
        elif fault == "wrong-kind":
            c.execute(
                select(
                    func.coinpup_validate_business_accrual(
                        row["journal_id"], "invoice_accrual", "payable.supplier", 1
                    )
                )
            )
        elif fault == "downgrade":
            with Operations.context(MigrationContext.configure(c)):
                document_accrual_head_migration()["downgrade"]()
        else:
            c.exec_driver_sql("SET CONSTRAINTS tr_document_accrual_head IMMEDIATE")
    assert err.value.orig.diag.constraint_name == constraint and snapshot(s) == before


def test_an_existing_document_transition_does_not_authorize_a_later_bare_cancellation(revision):
    s, h, doc, _, origin = revision
    prior = origin.postings[0]
    before = snapshot(s)
    with pytest.raises(IntegrityError) as err, s["engine"].begin() as c:
        first = append_accrual(c, h, doc, prior, "correct", 2)
        c.execute(select(func.coinpup_validate_operation_history(prior.operation_id)))
        current = SimpleNamespace(operation_id=prior.operation_id, journal_id=first["journal_id"])
        fact = line(h, doc)
        rogue = accrual(h, fact, current, "cancel") | dict(operation_version=3)
        post(c, cancelled(h), fact, rogue)
        existing = (
            c.execute(
                select(JournalLine.__table__).where(JournalLine.journal_id == first["journal_id"])
            )
            .mappings()
            .all()
        )
        c.execute(
            insert(JournalLine),
            [
                dict(r)
                | dict(id=uuid4(), journal_id=rogue["reversal_journal_id"], amount=-r["amount"])
                for r in existing
            ],
        )
        c.execute(
            update(FinancialOperation)
            .where(FinancialOperation.id == prior.operation_id)
            .values(version=3, status="cancelled")
        )
        c.execute(select(func.coinpup_validate_operation_history(prior.operation_id)))
    assert err.value.orig.diag.constraint_name == "ck_document_accrual_head"
    assert snapshot(s) == before


def test_historical_confirmation_survives_guarded_downgrade_and_upgrade(revision):
    from tests.integration.business.test_credit_revision_storage import definitions

    s, _, _, _, origin = revision
    before = snapshot(s)
    with s["engine"].begin() as c, Operations.context(MigrationContext.configure(c)):
        catalog = definitions(c)
        frozen = document_accrual_head_migration()
        frozen["downgrade"]()
        c.execute(select(func.coinpup_validate_business_confirmation(origin.id)))
        frozen["upgrade"]()
        assert definitions(c) == catalog
        c.execute(select(func.coinpup_validate_business_confirmation(origin.id)))
    assert snapshot(s) == before
