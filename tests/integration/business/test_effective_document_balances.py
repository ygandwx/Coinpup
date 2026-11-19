"""A document balance follows current prices, applications and releases within one snapshot."""

from types import SimpleNamespace
from uuid import uuid4

import pytest
from coinpup_api.business.allocation_return_schemas import BusinessAllocationReturnCreate
from coinpup_api.business.allocation_returns import BusinessAllocationReturnService
from coinpup_api.business.confirmation_reads import original_confirmation_detail
from coinpup_api.business.credit_reads import BusinessCreditReads, credit_document_capacity
from coinpup_api.business.document_revision_models import BusinessDocumentRevisionLine as Fact
from coinpup_api.business.settlement_balances import document_settlement_detail
from coinpup_api.business.settlement_models import BusinessAllocationLine
from coinpup_api.business.settlement_schemas import BusinessAllocationCreate
from coinpup_api.business.settlements import BusinessSettlementService
from coinpup_api.ledger.control_accounts import ControlAccounts
from coinpup_api.ledger.service import LedgerError
from sqlalchemy import event, func, insert, select
from sqlalchemy.orm import Session

from tests.integration.business.test_allocation_document_facts import source_payment
from tests.integration.business.test_confirmed_document_revision_headers import cancelled
from tests.integration.business.test_confirmed_document_revision_headers import revision as revision
from tests.integration.business.test_credit_document_sources import sources as sources
from tests.integration.business.test_credit_service import credit
from tests.integration.business.test_document_accrual_operation_heads import append_accrual
from tests.integration.business.test_document_revision_journals import confirmed
from tests.integration.business.test_document_revision_line_facts import line
from tests.integration.business.test_draft_service import drafts as drafts
from tests.integration.ledger.test_account_classes import snapshot
from tests.integration.ledger.test_posting_service_database import ledger_setup as ledger_setup

pytestmark = pytest.mark.integration


def test_repriced_capacity_subtracts_manual_credit_components_from_current_prices(sources):
    s, h, doc, origin, original, _, payload = sources
    tax_request = payload.model_copy(
        update={
            "id": uuid4(),
            "lines": [
                payload.lines[0].model_copy(update={"net_amount": "0", "tax_amount": "0.01"})
            ],
        }
    )
    tax_receipt = credit(s, tax_request, key="fictional-tax-only-credit")
    before = snapshot(s)
    with s["engine"].connect() as c, c.begin() as tx, Session(c) as db:
        append_accrual(
            c,
            h,
            doc,
            origin.postings[0],
            "correct",
            2,
            fact_changes=dict(
                quantity="1",
                unit_price="200",
                discount_amount="0",
                tax_rate_percent="10",
                net_amount="200",
                tax_amount="20",
                total_amount="220",
            ),
        )
        revised = credit_document_capacity(db, s["ledger"], doc.id)
        (row,) = revised.lines
        assert revised.expected_version == h["document_version"]
        assert (
            row.net_amount,
            row.tax_amount,
            row.total_amount,
            row.credited_net_amount,
            row.credited_tax_amount,
            row.remaining_net_amount,
            row.remaining_tax_amount,
            row.available_gross_amount,
        ) == ("200.00", "20.00", "220.00", "0.01", "0.01", "199.99", "19.99", "219.98")
        assert original_confirmation_detail(db, s["ledger"], doc.id).confirmation == origin
        tx.rollback()
    assert credit(s, payload) == original
    assert credit(s, tax_request, key="fictional-tax-only-credit") == tax_receipt
    assert snapshot(s) == before


def test_cross_asset_capacity_tracks_three_satoshis_through_return_and_document_cancellation(
    revision,
):
    s, h, doc, _, receipt = revision
    payment = source_payment(s, doc)
    with Session(s["engine"]) as db, db.begin():
        ControlAccounts(s["engine"]).ensure(
            db, s["owner"], s["ledger"], {"receivable.customer": {"BTC"}}
        )
    before = snapshot(s)
    with s["engine"].connect() as c, c.begin() as tx, Session(c) as db:

        def read():
            return document_settlement_detail(db, s["ledger"], doc.id)

        original = read()
        assert original.total_amount == doc.total_amount and original.asset_id == "USD"
        h = h | dict(asset_id="BTC", idempotency_key="fictional-effective-open-balance")
        change = dict(
            quantity="1",
            unit_price="0.00000003",
            tax_rate_percent="0",
            discount_amount="0",
            net_amount="0.00000003",
            tax_amount="0",
            total_amount="0.00000003",
            description="Fictional effective balance",
            project_id=None,
            project_snapshot=None,
        )
        priced = append_accrual(c, h, doc, receipt.postings[0], "correct", 2, fact_changes=change)
        zero = (
            line(h, doc)
            | change
            | dict(
                id=uuid4(),
                source_line_id=uuid4(),
                line_no=2,
                unit_price="0",
                net_amount="0",
                total_amount="0",
            )
        )
        c.execute(insert(Fact), zero)
        c.execute(select(func.coinpup_validate_operation_history(priced["operation_id"])))
        active = read()
        available = credit_document_capacity(db, s["ledger"], doc.id)
        assert (available.expected_version, available.asset_id, available.issue_date) == (
            h["document_version"],
            "BTC",
            h["issue_date"],
        )
        assert [
            (row.source_line_id, row.project_id, row.available_gross_amount)
            for row in available.lines
        ] == [(doc.lines[0].id, None, "0.00000003")]
        # Zero rows stay in the document and settlement view, but offer no credit capacity.
        assert credit_document_capacity(db, s["ledger"], doc.id, offset=1).lines == []
        assert (
            active.asset_id == "BTC"
            and active.total_amount == active.outstanding_amount == "0.00000003"
        )
        assert [(row.id, row.project_id, row.total_amount) for row in active.lines] == [
            (doc.lines[0].id, None, "0.00000003"),
            (zero["source_line_id"], None, "0.00000000"),
        ]
        allocation = BusinessAllocationCreate(
            id=uuid4(),
            payment_id=payment.id,
            allocation_date=doc.issue_date,
            lines=[
                dict(
                    document_id=doc.id,
                    document_line_id=doc.lines[0].id,
                    payment_amount="0.03",
                    document_amount="0.00000003",
                )
            ],
        )
        BusinessSettlementService(c).allocate(
            s["owner"], s["ledger"], allocation, "fictional-apply-three"
        )
        c.execute(select(func.coinpup_validate_business_allocation(allocation.id)))
        used = read()
        assert used.applied_amount == "0.00000003" and used.outstanding_amount == "0.00000000"
        assert (
            credit_document_capacity(db, s["ledger"], doc.id).lines[0].available_gross_amount
            == "0.00000000"
        )
        source = c.scalar(
            select(BusinessAllocationLine.id).where(
                BusinessAllocationLine.allocation_id == allocation.id
            )
        )
        for paid, applied, left in [
            ("0.01", "0.00000001", "0.00000002"),
            ("0.02", "0.00000002", "0.00000000"),
        ]:
            returned = BusinessAllocationReturnCreate(
                id=uuid4(),
                payment_id=payment.id,
                return_date=doc.issue_date,
                reason="Fictional controlled return",
                lines=[
                    dict(
                        source_allocation_line_id=source,
                        expected_version=1,
                        payment_amount=paid,
                        document_amount=applied,
                    )
                ],
            )
            BusinessAllocationReturnService(c).create(
                s["owner"], s["ledger"], returned, "fictional-return-" + paid
            )
            c.execute(select(func.coinpup_validate_business_allocation_return(returned.id)))
            assert read().applied_amount == left
            assert credit_document_capacity(db, s["ledger"], doc.id).lines[0].applied_amount == left
        assert read() == active
        assert credit_document_capacity(db, s["ledger"], doc.id) == available
        end = cancelled(h) | dict(
            id=uuid4(),
            previous_revision_id=h["id"],
            previous_version=h["document_version"],
            document_version=h["document_version"] + 1,
            idempotency_key="fictional-zero-balance",
        )
        append_accrual(c, end, doc, SimpleNamespace(**priced), "cancel", 3)
        c.execute(select(func.coinpup_validate_operation_history(priced["operation_id"])))
        closed = read()
        assert (
            closed.lines == () and closed.asset_id == "BTC" and closed.party_id == active.party_id
        )
        assert (
            closed.total_amount
            == closed.applied_amount
            == closed.outstanding_amount
            == "0.00000000"
        )
        empty = credit_document_capacity(db, s["ledger"], doc.id)
        assert empty.lines == [] and empty.asset_id == "BTC"
        assert empty.expected_version == end["document_version"]
        assert original_confirmation_detail(db, s["ledger"], doc.id).confirmation == receipt
        with pytest.raises(LedgerError) as denied:
            document_settlement_detail(db, uuid4(), doc.id)
        assert denied.value.status == 404
        with pytest.raises(LedgerError) as denied:
            credit_document_capacity(db, uuid4(), doc.id)
        assert denied.value.status == 404
        tx.rollback()
    assert snapshot(s) == before


@pytest.mark.parametrize("count", [1, 200])
def test_document_settlement_reads_scoped_prices_and_exposure_in_three_queries(drafts, count):
    s = drafts
    doc, postings = confirmed(s, "bill", count)
    with s["engine"].connect() as c, Session(c) as db:
        statements = []

        def capture(conn, cursor, sql, parameters, context, many):
            statements.append(sql)

        event.listen(c, "before_cursor_execute", capture)
        try:
            result = document_settlement_detail(db, s["ledger"], doc.id)
        finally:
            event.remove(c, "before_cursor_execute", capture)
        assert len(statements) == 3
        assert [row.id for row in result.lines] == [p.source_line_id for p in postings]
        assert (
            result.total_amount == result.outstanding_amount == ("2.00" if count == 200 else "0.01")
        )
        statements.clear()
        event.listen(c, "before_cursor_execute", capture)
        try:
            available = credit_document_capacity(db, s["ledger"], doc.id, limit=200)
        finally:
            event.remove(c, "before_cursor_execute", capture)
        assert len(statements) == 3
        assert [row.source_line_id for row in available.lines] == [
            p.source_line_id for p in postings
        ]
        assert all(row.available_gross_amount == "0.01" for row in available.lines)
    service = BusinessCreditReads(s["engine"])
    before = snapshot(s)
    assert service.capacity(s["owner"], s["ledger"], doc.id, limit=200) == available
    for owner, ledger in ((uuid4(), s["ledger"]), (s["owner"], uuid4())):
        with pytest.raises(LedgerError) as denied:
            service.capacity(owner, ledger, doc.id)
        assert denied.value.status == 404
    assert snapshot(s) == before
