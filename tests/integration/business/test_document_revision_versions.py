"""Read frozen, complete old/new facts and accrual identities inside one real PG snapshot."""

from uuid import uuid4

import pytest
from coinpup_api.business.document_revision_models import BusinessDocumentAccrual as Link
from coinpup_api.business.document_revision_models import BusinessDocumentRevision as Header
from coinpup_api.business.document_revision_models import BusinessDocumentRevisionLine as Fact
from coinpup_api.business.document_revision_versions import read_confirmed_document_version
from coinpup_api.ledger.service import LedgerError
from sqlalchemy import event, insert
from sqlalchemy.orm import Session

from tests.integration.business.test_confirmed_document_revision_headers import cancelled
from tests.integration.business.test_confirmed_document_revision_headers import revision as revision
from tests.integration.business.test_document_accrual_links import accrual, post
from tests.integration.business.test_document_revision_journals import confirmed
from tests.integration.business.test_document_revision_line_facts import line
from tests.integration.business.test_draft_service import drafts as drafts
from tests.integration.ledger.test_account_classes import snapshot
from tests.integration.ledger.test_posting_service_database import ledger_setup as ledger_setup

pytestmark = pytest.mark.integration


def test_original_fixed_current_and_terminal_document_facts_in_one_snapshot(revision):
    s, header, doc, _, receipt = revision
    before = snapshot(s)
    with s["engine"].connect() as c, c.begin() as tx, Session(c) as db:

        def read(version=None):
            return read_confirmed_document_version(db, s["ledger"], doc.id, version=version)

        old = read()
        assert old.version == receipt.document_version and old.revision_id is None
        assert old.original.confirmation == receipt and old.document == old.original.document
        c.execute(insert(Header), header)
        positive = line(header, doc) | dict(
            quantity="1.00",
            unit_price="40.05",
            tax_rate_percent="5.0",
            net_amount="40.05",
            tax_amount="2.00",
            total_amount="42.05",
        )
        zero = positive | dict(
            id=uuid4(),
            source_line_id=uuid4(),
            line_no=2,
            unit_price="-0.00",
            net_amount="0",
            tax_amount="0",
            total_amount="0",
            project_id=None,
            project_snapshot=None,
        )
        c.execute(insert(Fact), [positive, zero])
        first = accrual(header, positive, receipt.postings[0], "correct")
        post(c, header, positive, first)
        c.execute(insert(Link), first)
        active = read()
        assert read(old.version) == old
        assert active.original == old.original and active.revision_id == header["id"]
        assert active.version == old.version + 1
        assert [row.id for row in active.document.lines] == [
            positive["source_line_id"],
            zero["source_line_id"],
        ]
        assert [row.unit_price for row in active.document.lines] == ["40.05", "-0.00"]
        assert (
            active.document.total_amount == "42.05"
            and active.document.lines[1].project_snapshot is None
        )
        assert active.document.lines[0].category_snapshot == positive["category_snapshot"]
        assert (
            len(active.postings) == 1
            and active.postings[0].operation_id == receipt.postings[0].operation_id
        )
        assert (
            active.postings[0].version == 2 and active.postings[0].journal_id == first["journal_id"]
        )
        terminal = cancelled(header) | dict(
            id=uuid4(),
            idempotency_key="fictional-version-terminal",
            previous_revision_id=header["id"],
            previous_version=active.version,
            document_version=active.version + 1,
        )
        c.execute(insert(Header), terminal)
        final = accrual(terminal, positive, receipt.postings[0], "cancel") | dict(
            operation_version=3, previous_journal_id=first["journal_id"]
        )
        post(c, terminal, positive, final)
        c.execute(insert(Link), final)
        cancelled_result = read()
        assert cancelled_result.document is None and cancelled_result.postings == ()
        assert (
            cancelled_result.version == active.version + 1
            and cancelled_result.original == old.original
        )
        assert read(active.version) == active and read(old.version) == old
        tx.rollback()
    assert snapshot(s) == before


@pytest.mark.parametrize(
    "fault",
    [
        "omit-previous",
        "omit-positive",
        "incomplete-cancel",
        "missing-version",
        "draft-version",
        "boolean-version",
        "foreign-ledger",
        "foreign-document",
    ],
)
def test_missing_or_incomplete_versions_fail_closed(revision, fault):
    s, header, doc, _, receipt = revision
    before = snapshot(s)
    with s["engine"].connect() as c, c.begin() as tx, Session(c) as db:
        ledger, identifier, version = s["ledger"], doc.id, None
        if fault in ("omit-previous", "omit-positive", "incomplete-cancel"):
            c.execute(insert(Header), cancelled(header) if fault == "incomplete-cancel" else header)
            if fault != "incomplete-cancel":
                fact = line(header, doc)
                if fault == "omit-previous":
                    fact["source_line_id"] = uuid4()
                c.execute(insert(Fact), fact)
                if fault == "omit-previous":
                    row = accrual(header, fact, receipt.postings[0], "create")
                    post(c, header, fact, row)
                    c.execute(insert(Link), row)
        elif fault == "foreign-ledger":
            ledger = uuid4()
        elif fault == "foreign-document":
            identifier = uuid4()
        else:
            version = {"missing-version": 900, "draft-version": 1, "boolean-version": True}[fault]
        with pytest.raises(LedgerError) as err:
            read_confirmed_document_version(db, ledger, identifier, version=version)
        assert err.value.code == (
            "document_revision_integrity"
            if fault in ("omit-previous", "omit-positive", "incomplete-cancel")
            else "not_found"
        )
        tx.rollback()
    assert snapshot(s) == before


@pytest.mark.parametrize("count", [1, 200])
def test_full_replacement_facts_read_in_a_constant_number_of_queries(revision, count):
    s, pattern, *_ = revision
    # A separate real 200-line initial confirmation exercises both bounded collections.
    # The regular fixture already owns its original key. Give the second document its own key.
    doc, operations = confirmed(s, "bill", count, key="fictional-full-projection")
    header = pattern | dict(
        id=uuid4(),
        document_id=doc.id,
        confirmation_id=doc.confirmation_id,
        previous_version=doc.version,
        document_version=doc.version + 1,
        idempotency_key="fictional-projection",
        party_id=doc.party_id,
        party_snapshot=doc.party_snapshot,
        asset_id=doc.asset_id,
        issue_date=doc.issue_date,
    )
    before = snapshot(s)
    with s["engine"].connect() as c, c.begin() as tx, Session(c) as db:
        c.execute(insert(Header), header)
        facts = []
        links = []
        for source, old in zip(doc.lines, operations, strict=True):
            f = line(header, doc) | dict(
                id=uuid4(), source_line_id=source.id, line_no=source.line_no
            )
            facts.append(f)
            links.append(accrual(header, f, old, "correct"))
        c.execute(insert(Fact), facts)
        for fact, link in zip(facts, links, strict=True):
            post(c, header, fact, link)
        c.execute(insert(Link), links)
        selected = []

        def count_query(_conn, _cursor, stmt, *_args):
            if stmt.lstrip().upper().startswith("SELECT"):
                selected.append(stmt)

        event.listen(c, "before_cursor_execute", count_query)
        try:
            version = read_confirmed_document_version(db, doc.ledger_id, doc.id)
        finally:
            event.remove(c, "before_cursor_execute", count_query)
        assert len(selected) == 8
        assert len(version.postings) == len(version.document.lines) == count
        assert all(p.version == 2 for p in version.postings)
        assert version.document.total_amount == doc.total_amount
        tx.rollback()
    assert snapshot(s) == before
