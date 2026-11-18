"""Verify immutable original and revised postings while the complete group gate is closed."""

from datetime import timedelta
from uuid import uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from coinpup_api.business.document_revision_journals import prepare_document_revision_journals
from coinpup_api.business.document_revision_models import BusinessDocumentAccrual as Link
from coinpup_api.business.document_revision_models import BusinessDocumentRevision as Header
from coinpup_api.business.document_revision_models import BusinessDocumentRevisionLine as Fact
from coinpup_api.business.document_revision_schemas import ConfirmedDocumentCorrection
from coinpup_api.ledger.assets import ASSETS
from coinpup_api.ledger.control_accounts import ControlAccounts
from coinpup_api.ledger.models import Journal, JournalLine
from sqlalchemy import func, insert, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from tests.integration.business.test_confirmation_service import prepare, submit
from tests.integration.business.test_confirmed_document_revision_headers import cancelled
from tests.integration.business.test_confirmed_document_revision_headers import revision as revision
from tests.integration.business.test_document_accrual_links import accrual, post
from tests.integration.business.test_document_revision_capacity import replacement
from tests.integration.business.test_document_revision_journals import confirmed
from tests.integration.business.test_document_revision_line_facts import line
from tests.integration.business.test_draft_service import drafts as drafts
from tests.integration.conftest import document_accrual_fact_migration
from tests.integration.ledger.test_account_classes import snapshot
from tests.integration.ledger.test_posting_service_database import ledger_setup as ledger_setup

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("kind", ["invoice", "bill"])
@pytest.mark.parametrize("asset,unit", [("USD", "0.01"), ("BTC", "0.00000001")])
def test_historical_accrual_matches_its_immutable_confirmation(drafts, kind, asset, unit):
    doc, command = prepare(
        drafts,
        kind=kind,
        changes={"asset_id": asset},
        line_changes={"unit_price": unit, "tax_rate_percent": "0"},
    )
    origin = submit(drafts, doc, command)
    before = snapshot(drafts)
    with drafts["engine"].begin() as c:
        for member in origin.postings:
            c.execute(select(func.coinpup_validate_document_accrual_fact(member.journal_id)))
    assert submit(drafts, doc, command) == origin and snapshot(drafts) == before


@pytest.mark.parametrize(
    "action,fault,constraint",
    [
        ("correct", None, "ck_document_revision_complete"),
        ("create", None, "ck_document_revision_complete"),
        ("cancel", None, "ck_document_revision_complete"),
        ("correct", "missing-line", "ck_document_accrual_fact"),
        ("correct", "other-total", "ck_document_accrual_fact"),
        ("correct", "opposite-roles", "ck_document_accrual_fact"),
        ("correct", "missing-project", "ck_document_accrual_fact"),
        ("correct", "missing-party", "ck_document_accrual_fact"),
        ("correct", "wrong-reversal", "ck_journal_reversal"),
        ("correct", "reversal-loses-party", "ck_journal_reversal_dimensions"),
        ("correct", "downgrade", "ck_document_accrual_fact_downgrade"),
    ],
)
def test_revision_posting_rows_match_the_priced_version(revision, action, fault, constraint):
    s, header, doc, _, origin = revision
    prior = origin.postings[0]
    before = snapshot(s)
    with pytest.raises(IntegrityError) as failed, s["engine"].begin() as c:
        if action == "cancel":
            header = cancelled(header)
        c.execute(insert(Header), header)
        fact = line(header, doc)
        if action == "create":
            fact["source_line_id"] = uuid4()
        if action != "cancel":
            c.execute(insert(Fact), fact)
        link = accrual(header, fact, prior, action)
        post(c, header, fact, link)
        source = (
            c.execute(
                select(JournalLine.__table__)
                .where(JournalLine.journal_id == prior.journal_id)
                .order_by(JournalLine.line_no)
            )
            .mappings()
            .all()
        )
        if link["reversal_journal_id"]:
            opposite = [
                dict(r)
                | dict(id=uuid4(), journal_id=link["reversal_journal_id"], amount=-r["amount"])
                for r in source
            ]
            if fault == "wrong-reversal":
                for row in opposite:
                    row["amount"] *= 2
            if fault == "reversal-loses-party":
                for row in opposite:
                    row["party_id"] = None
            c.execute(insert(JournalLine), opposite)
        if link["journal_id"]:
            normal = [
                dict(r)
                | dict(
                    id=uuid4(),
                    journal_id=link["journal_id"],
                    document_line_id=fact["source_line_id"],
                )
                for r in source
            ]
            if fault == "missing-line":
                normal.pop()
            for row in normal:
                if fault == "other-total":
                    row["amount"] *= 2
                if fault == "opposite-roles":
                    row["amount"] = -row["amount"]
                if fault == "missing-project":
                    row["project_id"] = None
                if fault == "missing-party":
                    row["party_id"] = None
            c.execute(insert(JournalLine), normal)
        c.execute(insert(Link), link)
        if fault == "downgrade":
            with Operations.context(MigrationContext.configure(c)):
                document_accrual_fact_migration()["downgrade"]()
        c.exec_driver_sql(
            "SET CONSTRAINTS tr_document_accrual_journals, tr_document_accrual_facts,"
            " tr_journals_reversal_dimensions IMMEDIATE"
        )
        c.exec_driver_sql("SET CONSTRAINTS tr_document_revisions_complete IMMEDIATE")
    assert failed.value.orig.diag.constraint_name == constraint
    assert snapshot(s) == before


@pytest.mark.parametrize("kind", ["invoice", "bill"])
def test_sealed_usd_origin_and_repriced_btc_replacement_are_valid_together(revision, kind):
    s, template, *_ = revision
    doc, old = confirmed(s, kind, 1, key="fictional-independent-currency")
    key = "receivable.customer" if kind == "invoice" else "payable.supplier"
    with Session(s["engine"]) as db, db.begin():
        ControlAccounts(s["engine"]).ensure(db, s["owner"], s["ledger"], {key: {"BTC"}})
    issue = doc.issue_date + timedelta(days=7)
    recognition = doc.lines[0].recognition_date + timedelta(days=2)
    changed = dict(
        unit_price="0.00000003",
        description="Fictional revised accrual",
        recognition_date=recognition,
    )
    cmd = ConfirmedDocumentCorrection(
        expected_version=doc.version,
        reason="Fictional credit terms",
        replacement=replacement(doc, {doc.lines[0].id: changed}, asset_id="BTC", issue_date=issue),
    )
    h = template | dict(
        id=uuid4(),
        confirmation_id=doc.confirmation_id,
        document_id=doc.id,
        asset_id="BTC",
        issue_date=issue,
        reason=cmd.reason,
        previous_version=doc.version,
        document_version=doc.version + 1,
        idempotency_key="fictional-revised-currency",
    )
    before = snapshot(s)
    with pytest.raises(IntegrityError) as failed, s["engine"].begin() as c, Session(c) as db:
        member = prepare_document_revision_journals(
            db, doc, cmd, old, ASSETS["USD"], ASSETS["BTC"]
        ).members[0]
        c.execute(insert(Header), h)
        fact = (
            line(h, doc)
            | changed
            | dict(net_amount="0.00000003", tax_amount="0", total_amount="0.00000003")
        )
        c.execute(insert(Fact), fact)
        row = accrual(h, fact, old[0], "correct") | dict(
            reversal_journal_id=member.reversal.header["id"],
            journal_id=member.replacement.header["id"],
        )
        for prepared in (member.reversal, member.replacement):
            c.execute(insert(Journal), prepared.header)
            c.execute(insert(JournalLine), list(prepared.parameters))
        c.execute(insert(Link), row)
        c.exec_driver_sql(
            "SET CONSTRAINTS tr_document_accrual_journals,tr_document_accrual_facts,"
            "tr_journals_reversal_dimensions IMMEDIATE"
        )
        c.execute(select(func.coinpup_validate_document_accrual_fact(old[0].journal_id)))
        c.execute(select(func.coinpup_validate_document_accrual_fact(row["journal_id"])))
        c.exec_driver_sql("SET CONSTRAINTS tr_document_revisions_complete IMMEDIATE")
    assert failed.value.orig.diag.constraint_name == "ck_document_revision_complete"
    assert snapshot(s) == before
