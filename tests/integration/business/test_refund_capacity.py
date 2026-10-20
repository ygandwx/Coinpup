"""Fictional refund principal stays distinct in existing reads and future allocations."""

from uuid import uuid4

import pytest
from coinpup_api.business.settlement_reads import BusinessSettlementReads
from coinpup_api.business.settlement_schemas import BusinessAllocationCreate
from coinpup_api.business.settlements import BusinessSettlementService
from coinpup_api.ledger.service import LedgerError
from sqlalchemy import event

from tests.integration.business.test_draft_service import drafts as drafts
from tests.integration.business.test_refund_validation import fixture, write
from tests.integration.ledger.test_account_classes import snapshot
from tests.integration.ledger.test_posting_service_database import ledger_setup as ledger_setup

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("direction", ["receipt", "payout"])
def test_reads_and_new_allocations_subtract_refunds_without_rewriting_original_receipts(
    drafts, direction
):
    s = drafts
    source = fixture(s, direction)
    reads = BusinessSettlementReads(s["engine"])
    before = reads.get_payment(s["owner"], s["ledger"], source[0].id)
    assert (before.applied_amount, before.unapplied_amount) == ("80.00", "20.00")
    before_list = reads.list_payments(s["owner"], s["ledger"])
    write(s, source, asset="EUR", amount="9.37", principal="10")
    after = reads.get_payment(s["owner"], s["ledger"], source[0].id)
    assert after.model_dump() == before.model_dump() | {"unapplied_amount": "10.00"}
    assert [r.model_dump() for r in reads.list_payments(s["owner"], s["ledger"])] == [
        row.model_dump() | {"unapplied_amount": "10.00"} for row in before_list
    ]
    assert after.receipt == before.receipt
    original = source[1]

    def request(amount):
        return BusinessAllocationCreate.model_validate(
            original.model_dump()
            | {
                "id": uuid4(),
                "allocation_date": "2026-10-02",
                "lines": [
                    original.lines[0].model_dump()
                    | {"payment_amount": amount, "document_amount": amount}
                ],
            }
        )

    unchanged = snapshot(s)
    service = BusinessSettlementService(s["engine"])
    with pytest.raises(LedgerError) as error:
        service.allocate(s["owner"], s["ledger"], request("10.01"), str(uuid4()))
    assert error.value.code == "allocation_exceeds_payment"
    assert snapshot(s) == unchanged
    service.allocate(s["owner"], s["ledger"], request("10"), str(uuid4()))
    current = reads.get_payment(s["owner"], s["ledger"], source[0].id)
    assert current.applied_amount == "90.00" and current.unapplied_amount == "0.00"
    assert current.receipt == before.receipt


def test_refund_aggregation_keeps_payment_page_query_count_constant(drafts):
    s = drafts
    source = fixture(s)
    reads = BusinessSettlementReads(s["engine"])
    statements = []

    def capture(_conn, _cursor, statement, _parameters, _context, _many):
        if statement.lstrip().upper().startswith("SELECT"):
            statements.append(statement)

    event.listen(s["engine"], "before_cursor_execute", capture)
    try:
        reads.list_payments(s["owner"], s["ledger"], limit=25)
        before = len(statements)
        write(s, source, principal="10", amount="10")
        statements.clear()
        reads.list_payments(s["owner"], s["ledger"], limit=25)
        assert len(statements) == before == 5
    finally:
        event.remove(s["engine"], "before_cursor_execute", capture)
