"""Already-versioned allocation/return commands work after real whole-document commits."""

from decimal import Decimal
from uuid import uuid4

import pytest
from coinpup_api.business.allocation_return_schemas import BusinessAllocationReturnCreate
from coinpup_api.business.document_revision_models import BusinessDocumentRevisionLine
from coinpup_api.business.document_revision_schemas import ConfirmedDocumentCancellation
from coinpup_api.business.settlement_models import BusinessAllocationLine
from coinpup_api.business.settlement_schemas import BusinessPaymentCreate
from coinpup_api.ledger.models import JournalLine
from coinpup_api.ledger.schemas import EntityUpdate
from coinpup_api.ledger.service import LedgerError
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from tests.integration.business.test_allocation_return_service import submit_return
from tests.integration.business.test_document_revision_capacity import replacement
from tests.integration.business.test_document_revision_commands import correction, send
from tests.integration.business.test_document_revision_journals import confirmed
from tests.integration.business.test_document_revision_storage import reviewed
from tests.integration.business.test_draft_service import drafts as drafts
from tests.integration.business.test_settlement_service import pay
from tests.integration.ledger.test_account_classes import snapshot
from tests.integration.ledger.test_period_transactions import change as change_period
from tests.integration.ledger.test_posting_service_database import ledger_setup as ledger_setup
from tests.integration.ledger.test_posting_service_database import opening

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("kind", ["invoice", "bill"])
@pytest.mark.parametrize(
    "asset,unit,count",
    [
        ("USD", "0.01", 1),
        ("BTC", "0.00000001", 1),
        ("ETH", "0.000000000000000001", 1),
        ("USD", "0.01", 200),
    ],
)
def test_committed_new_sources_pin_allocations_and_require_explicit_returns_before_cancel(
    drafts, kind, asset, unit, count
):
    s = drafts
    s["posting"].post_opening(s["owner"], s["ledger"], opening(s, "10", "USD"), "fictional-cash")
    doc, _ = confirmed(s, kind, count)
    rows = [
        r.model_copy(update={"id": uuid4(), "unit_price": unit})
        for r in reversed(replacement(doc).lines)
    ]
    command = correction(doc).model_copy(
        update={"replacement": reviewed(doc, rows, asset_id=asset)}
    )
    fixed = send(s, doc, command, "fictional-allocation-document-correct")
    payment = BusinessPaymentCreate(
        id=uuid4(),
        direction="receipt" if kind == "invoice" else "payout",
        party_id=doc.party_id,
        account_id=s["account"].id,
        asset_id="USD",
        amount=f"{count // 100}.{count % 100:02}",
        transaction_date=doc.issue_date,
        description="Fictional current-source cash",
        fees=[
            dict(
                account_id=s["account"].id,
                asset_id="USD",
                amount="0.01",
                category_id=s["expenses"][0].id,
            )
        ],
        initial_allocation=dict(
            id=uuid4(),
            allocation_date=doc.issue_date,
            lines=[
                dict(
                    document_id=doc.id,
                    document_line_id=r.id,
                    payment_amount="0.01",
                    document_amount=unit,
                )
                for r in rows
            ],
        ),
    )
    paid = pay(s, payment)
    assert len(paid.initial_allocation.postings) == count
    with Session(s["engine"]) as db:
        pinned = db.scalars(
            select(BusinessAllocationLine)
            .where(BusinessAllocationLine.allocation_id == paid.initial_allocation.id)
            .order_by(BusinessAllocationLine.line_no)
        ).all()
        facts = {
            f.id: f
            for f in db.scalars(
                select(BusinessDocumentRevisionLine).where(
                    BusinessDocumentRevisionLine.revision_id == fixed.revision_id
                )
            )
        }
        assert len(pinned) == len(facts) == count
        for row, source in zip(pinned, rows, strict=True):
            fact = facts[row.document_revision_line_id]
            assert (fact.source_line_id, fact.document_version) == (source.id, fixed.version)
            assert row.document_amount == Decimal(unit) and row.payment_amount == Decimal("0.01")
        assert all(
            total == 0
            for _, total in db.execute(
                select(JournalLine.asset_id, func.sum(JournalLine.amount)).group_by(
                    JournalLine.asset_id
                )
            )
        )
    ending = ConfirmedDocumentCancellation(
        expected_version=fixed.version, reason="Fictional explicit document end"
    )
    before = snapshot(s)
    with pytest.raises(LedgerError) as used:
        send(s, doc, ending, "fictional-allocation-document-end")
    assert used.value.code == "document_revision_dependency" and snapshot(s) == before
    returning = BusinessAllocationReturnCreate(
        id=uuid4(),
        payment_id=payment.id,
        return_date=doc.issue_date,
        reason="Fictional explicit release of actual current-source applications",
        lines=[
            dict(
                source_allocation_line_id=r.id,
                expected_version=1,
                payment_amount="0.01",
                document_amount=unit,
            )
            for r in pinned
        ],
    )
    returned = submit_return(s, returning)
    ended = send(s, doc, ending, "fictional-allocation-document-end")
    change_period(s, cutoff="2026-11-06")
    s["structure"].update_entity(
        s["owner"], s["entity"].id, EntityUpdate(expected_version=1, archived=True)
    )
    before = snapshot(s)
    assert pay(s, payment) == paid
    assert submit_return(s, returning) == returned
    assert send(s, doc, command, "fictional-allocation-document-correct") == fixed
    assert send(s, doc, ending, "fictional-allocation-document-end") == ended
    assert snapshot(s) == before
