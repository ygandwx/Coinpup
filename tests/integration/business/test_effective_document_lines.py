"""Current allocation sources come from the effective version under one ledger snapshot."""

from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest
from coinpup_api.business.document_revision_models import BusinessDocumentAccrual as Link
from coinpup_api.business.document_revision_models import BusinessDocumentRevision as Header
from coinpup_api.business.document_revision_models import BusinessDocumentRevisionLine as Fact
from coinpup_api.business.effective_document_lines import effective_document_lines
from coinpup_api.business.settlement_schemas import BusinessPaymentCreate, InitialAllocationCreate
from coinpup_api.business.settlements import allocation_plan, source_rows
from coinpup_api.ledger.assets import ASSETS
from coinpup_api.ledger.control_accounts import ControlAccounts
from coinpup_api.ledger.errors import MoneyError
from coinpup_api.ledger.models import FinancialOperation, JournalLine
from coinpup_api.ledger.service import LedgerError
from sqlalchemy import event, func, insert, select, update
from sqlalchemy.orm import Session

from tests.integration.business.test_confirmed_document_revision_headers import cancelled
from tests.integration.business.test_confirmed_document_revision_headers import revision as revision
from tests.integration.business.test_document_accrual_links import accrual, post
from tests.integration.business.test_document_accrual_operation_heads import append_accrual
from tests.integration.business.test_document_revision_journals import confirmed
from tests.integration.business.test_document_revision_line_facts import line
from tests.integration.business.test_draft_service import drafts as drafts
from tests.integration.ledger.test_account_classes import snapshot
from tests.integration.ledger.test_posting_service_database import ledger_setup as ledger_setup

pytestmark = pytest.mark.integration


def request(day, pairs, *, amount="0.01"):
    return InitialAllocationCreate(
        id=uuid4(),
        allocation_date=day,
        lines=[
            dict(
                document_id=doc,
                document_line_id=source,
                payment_amount="0.01",
                document_amount=amount,
            )
            for doc, source in pairs
        ],
    )


@pytest.mark.parametrize("count", [1, 200])
def test_requested_original_sources_use_one_select_for_one_or_two_hundred(drafts, count):
    s = drafts
    doc, old = confirmed(s, "bill", count)
    ask = request(doc.issue_date, [(doc.id, r.source_line_id) for r in reversed(old)])
    before = snapshot(s)
    with s["engine"].connect() as c, Session(c) as db:
        statements = []
        event.listen(
            c, "before_cursor_execute", lambda _c, _cur, sql, *args: statements.append(sql)
        )
        rows = source_rows(db, s["ledger"], ask)
        assert len(statements) == 1 and len(rows) == count
        assert [
            (
                rows[p.source_line_id][1].operation_id,
                rows[p.source_line_id][1].journal_id,
                rows[p.source_line_id][1].total_amount,
            )
            for p in old
        ] == [(p.operation_id, p.journal_id, Decimal("0.01")) for p in old]
        assert {d.version for d, _ in rows.values()} == {doc.version}
        assert source_rows(db, s["ledger"], None) == {}
        assert len(statements) == 1
        for ledger, document, lines in (
            (uuid4(), doc.id, None),
            (s["ledger"], None, None),
            (s["ledger"], None, []),
            (s["ledger"], doc.id, []),
            (s["ledger"], uuid4(), [old[0].source_line_id]),
        ):
            projection = effective_document_lines(ledger, document_id=document, line_ids=lines)
            assert db.execute(select(projection)).all() == []
    assert snapshot(s) == before


def test_cross_asset_replacement_and_cancellation_select_only_current_rows(revision):
    s, h, doc, _, origin = revision
    other, other_posted = confirmed(s, "bill", 1, key="fictional-unrelated-origin")
    removed = origin.postings[0].line_id
    day = doc.issue_date + timedelta(days=5)
    h = h | dict(asset_id="BTC", issue_date=day, idempotency_key="fictional-effective-sources")
    with Session(s["engine"]) as db, db.begin():
        ControlAccounts(s["engine"]).ensure(
            db, s["owner"], s["ledger"], {"receivable.customer": {"BTC"}}
        )
    before = snapshot(s)
    with s["engine"].connect() as c, c.begin() as tx, Session(c) as db:
        c.execute(insert(Header), h)
        positive = line(h, doc) | dict(
            source_line_id=uuid4(),
            description="Fictional revised source",
            unit_price="0.00000003",
            tax_rate_percent="0",
            discount_amount="0",
            net_amount="0.00000003",
            tax_amount="0",
            total_amount="0.00000003",
        )
        zero = positive | dict(
            id=uuid4(),
            source_line_id=uuid4(),
            line_no=2,
            unit_price="0",
            net_amount="0",
            total_amount="0",
        )
        c.execute(insert(Fact), [positive, zero])
        projection = effective_document_lines(s["ledger"], document_id=doc.id)
        assert db.scalars(select(projection.c.source_line_id)).all() == [zero["source_line_id"]]
        prior = origin.postings[0]
        create = accrual(h, positive, prior, "create")
        retire = accrual(h, positive | dict(source_line_id=removed), prior, "cancel")
        post(c, h, positive, create)
        post(c, h, positive, retire)
        old_lines = (
            c.execute(
                select(JournalLine.__table__).where(JournalLine.journal_id == prior.journal_id)
            )
            .mappings()
            .all()
        )
        c.execute(
            insert(JournalLine),
            [
                dict(r)
                | dict(id=uuid4(), journal_id=retire["reversal_journal_id"], amount=-r["amount"])
                for r in old_lines
            ],
        )
        c.execute(
            insert(JournalLine),
            [
                dict(r)
                | dict(
                    id=uuid4(),
                    journal_id=create["journal_id"],
                    asset_id="BTC",
                    document_line_id=positive["source_line_id"],
                    amount=Decimal("0.00000003")
                    if r["role"] == "account"
                    else Decimal("-0.00000003"),
                )
                for r in old_lines
            ],
        )
        c.execute(insert(Link), [create, retire])
        c.execute(
            update(FinancialOperation)
            .where(FinancialOperation.id == prior.operation_id)
            .values(version=2, status="cancelled")
        )
        for op in (create["operation_id"], retire["operation_id"]):
            c.execute(select(func.coinpup_validate_operation_history(op)))
        amounts = db.execute(select(projection).order_by(projection.c.line_no)).all()
        assert [
            (r.source_line_id, r.net_amount, r.document_version, r.asset_id) for r in amounts
        ] == [
            (positive["source_line_id"], Decimal("0.00000003"), h["document_version"], "BTC"),
            (zero["source_line_id"], Decimal("0"), h["document_version"], "BTC"),
        ]
        ask = request(day, [(doc.id, positive["source_line_id"])], amount="0.00000003")
        sources = source_rows(db, s["ledger"], ask)
        assert sources[positive["source_line_id"]][1].description == "Fictional revised source"
        payer = BusinessPaymentCreate(
            id=uuid4(),
            party_id=doc.party_id,
            direction="receipt",
            account_id=s["account"].id,
            asset_id="USD",
            amount="0.01",
            transaction_date=day,
            description="Fictional revised capacity",
            initial_allocation=ask,
        )
        plan = allocation_plan(db, s["ledger"], payer, ask, sources, ASSETS, new_payment=True)
        assert plan.lines[0].document_amount.minor_units == 3
        too_much = request(day, [(doc.id, positive["source_line_id"])], amount="0.00000004")
        with pytest.raises(MoneyError) as over:
            allocation_plan(db, s["ledger"], payer, too_much, sources, ASSETS, new_payment=True)
        assert over.value.code == "allocation_exceeds_document"
        mixed = request(
            day, [(doc.id, positive["source_line_id"]), (other.id, other_posted[0].source_line_id)]
        )
        both = source_rows(db, s["ledger"], mixed)
        assert {d.asset_id for d, _ in both.values()} == {"USD", "BTC"}
        with pytest.raises(LedgerError) as old:
            source_rows(db, s["ledger"], request(day, [(doc.id, removed)]))
        assert old.value.code == "not_found"
        terminal = cancelled(h) | dict(
            id=uuid4(),
            previous_revision_id=h["id"],
            previous_version=h["document_version"],
            document_version=h["document_version"] + 1,
            idempotency_key="fictional-terminal-sources",
        )
        current = SimpleNamespace(
            operation_id=create["operation_id"], journal_id=create["journal_id"]
        )
        append_accrual(c, terminal, doc, current, "cancel", 2, source_id=positive["source_line_id"])
        c.execute(select(func.coinpup_validate_operation_history(current.operation_id)))
        assert db.execute(select(projection)).all() == []
        with pytest.raises(LedgerError) as obsolete:
            source_rows(db, s["ledger"], ask)
        assert obsolete.value.code == "not_found"
        unaffected = source_rows(
            db, s["ledger"], request(day, [(other.id, other_posted[0].source_line_id)])
        )
        assert (
            len(unaffected) == 1 and unaffected[other_posted[0].source_line_id][0].asset_id == "USD"
        )
        tx.rollback()
    assert snapshot(s) == before
