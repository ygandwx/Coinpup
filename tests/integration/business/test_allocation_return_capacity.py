"""Fictional returns release exact capacity while original receipts stay unchanged."""

from decimal import Decimal, localcontext
from uuid import UUID, uuid4

import pytest
from coinpup_api.business.settlement_models import BusinessAllocationLine
from coinpup_api.business.settlement_reads import BusinessSettlementReads
from coinpup_api.business.settlement_schemas import BusinessAllocationCreate
from coinpup_api.business.settlement_totals import applied_totals
from coinpup_api.business.settlements import BusinessSettlementService
from coinpup_api.ledger.service import LedgerError
from sqlalchemy import event, select

from tests.integration.business.test_allocation_return_storage import another_return, pending, rows
from tests.integration.business.test_draft_service import drafts as drafts
from tests.integration.ledger.test_posting_service_database import ledger_setup as ledger_setup

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("direction", ["receipt", "payout"])
@pytest.mark.parametrize("cross_asset", [False, True])
def test_reads_and_new_allocations_share_net_capacity_without_rewriting_receipts(
    drafts, direction, cross_asset
):
    s = drafts
    values = rows(
        s,
        direction=direction,
        document_asset="EUR" if cross_asset else "USD",
        source_document_amount="100" if cross_asset else "110",
        return_payment_amount="22" if cross_asset else "20",
    )
    payment, _, document, _ = values["facts"]
    reads = BusinessSettlementReads(s["engine"])
    service = BusinessSettlementService(s["engine"])
    args = s["owner"], s["ledger"]
    with s["engine"].connect() as c:
        allocation_id = c.scalar(
            select(BusinessAllocationLine.allocation_id).where(
                BusinessAllocationLine.id == values["member"]["source_allocation_line_id"]
            )
        )
    before = reads.get_payment(*args, payment.id)
    original = reads.get_allocation(*args, allocation_id)
    assert before.applied_amount == "110.00"
    assert before.unapplied_amount == "0.00"
    assert original.payment_amount == "110.00"
    with s["engine"].begin() as c:
        pending(c, values)
    with localcontext() as context:
        context.prec = 2  # Net sums are performed exactly in PostgreSQL, not Python Decimal.
        detail = reads.get_payment(*args, payment.id)
        summary = reads.list_payments(*args)[0]
        capacity = reads.document_capacity(*args, document.id)
    expected_used = "88.00" if cross_asset else "90.00"
    expected_free = "22.00" if cross_asset else "20.00"
    assert (detail.applied_amount, summary.applied_amount) == (expected_used,) * 2
    assert (detail.unapplied_amount, summary.unapplied_amount) == (expected_free,) * 2
    assert detail.receipt == before.receipt
    assert reads.get_allocation(*args, allocation_id) == original
    assert reads.list_allocations(*args)[0].payment_amount == "110.00"
    assert capacity.applied_amount == ("80.00" if cross_asset else "90.00")
    assert capacity.outstanding_amount == ("30.00" if cross_asset else "20.00")
    assert capacity.lines[0].applied_amount == capacity.applied_amount
    payload = BusinessAllocationCreate(
        id=uuid4(),
        payment_id=payment.id,
        allocation_date="2026-10-01",
        lines=[
            dict(
                document_id=document.id,
                document_line_id=document.lines[0].id,
                payment_amount=expected_free,
                document_amount="20.00",
            )
        ],
    )
    result = service.allocate(*args, payload, "fictional-reapply")
    assert reads.get_payment(*args, payment.id).unapplied_amount == "0.00"
    assert reads.document_capacity(*args, document.id).outstanding_amount == (
        "10.00" if cross_asset else "0.00"
    )
    assert service.allocate(*args, payload, "fictional-reapply") == result
    assert reads.get_payment(*args, payment.id).receipt == before.receipt
    assert reads.get_allocation(*args, allocation_id) == original
    with pytest.raises(LedgerError) as error:
        service.allocate(
            *args, payload.model_copy(update={"id": uuid4()}), "fictional-over-capacity"
        )
    assert error.value.status == 422


def test_multiple_returns_are_subtracted_once_and_only_within_the_requested_scope(drafts):
    s = drafts
    values = rows(s, document_asset="EUR", source_document_amount="100", return_payment_amount="22")
    with s["engine"].begin() as c:
        pending(c, values)
    residual = another_return(values, payment_amount="88", document_amount="80", source_return_no=2)
    with s["engine"].begin() as c:
        pending(c, residual)
    payment, _, document, _ = values["facts"]
    with s["engine"].connect() as c:
        for by_document_line, identifier in (
            (False, payment.id),
            (True, document.lines[0].id),
        ):
            assert applied_totals(
                c, s["ledger"], {identifier}, by_document_line=by_document_line
            ) == {identifier: Decimal(0)}
            assert applied_totals(c, uuid4(), {identifier}, by_document_line=by_document_line) == {}
            assert (
                applied_totals(c, s["ledger"], {uuid4()}, by_document_line=by_document_line) == {}
            )
    reads = BusinessSettlementReads(s["engine"])
    assert reads.get_payment(s["owner"], s["ledger"], payment.id).unapplied_amount == "110.00"
    assert (
        reads.document_capacity(s["owner"], s["ledger"], document.id).outstanding_amount == "110.00"
    )


@pytest.mark.parametrize("by_document_line", [False, True])
def test_net_aggregation_is_one_bounded_query_not_one_per_source(drafts, by_document_line):
    s = drafts
    statements = []

    def capture(_connection, _cursor, statement, *_rest):
        statements.append(statement)

    with s["engine"].connect() as c:
        event.listen(c, "before_cursor_execute", capture)
        assert applied_totals(c, s["ledger"], [], by_document_line=by_document_line) == {}
        assert not statements
        assert (
            applied_totals(
                c,
                s["ledger"],
                [UUID(int=i) for i in range(1, 201)],
                by_document_line=by_document_line,
            )
            == {}
        )
        assert len(statements) == 1
