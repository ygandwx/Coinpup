"""Fictional scoped settlement balances, bounded queries and concurrent snapshots."""

from uuid import uuid4

import pytest
from coinpup_api.business.settlement_balances import BusinessSettlementBalances
from coinpup_api.ledger.schemas import AssetUpdate, EntityCreate, EntityUpdate
from coinpup_api.ledger.service import LedgerError
from sqlalchemy import event

from tests.integration.business.test_confirmation_service import prepare, submit
from tests.integration.business.test_draft_service import drafts as drafts
from tests.integration.business.test_settlement_service import allocate, pay, prepared
from tests.integration.ledger.test_account_classes import snapshot
from tests.integration.ledger.test_posting_service_database import ledger_setup as ledger_setup

pytestmark = pytest.mark.integration


def test_document_capacity_uses_gross_total_and_keeps_zero_source_lines(drafts):
    s = drafts
    first = s["payload"]().model_dump()["lines"][0]
    first.update(quantity="1", unit_price="100", discount_amount="0", tax_rate_percent="10")
    zero = first | dict(id=uuid4(), unit_price="0")
    saved, request = prepare(s, changes={"lines": [first, zero]})
    reads = BusinessSettlementBalances(s["engine"])
    with pytest.raises(LedgerError) as error:
        reads.document_capacity(s["owner"], s["ledger"], saved.id)
    assert error.value.status == 404
    submit(s, saved, request)
    current = reads.document_capacity(s["owner"], s["ledger"], saved.id)
    assert current.total_amount == current.outstanding_amount == "110.00"
    assert [(row.total_amount, row.applied_amount) for row in current.lines] == [
        ("110.00", "0.00"),
        ("0.00", "0.00"),
    ]


@pytest.mark.parametrize("direction", ["receipt", "payout"])
@pytest.mark.parametrize("asset", ["USD", "EUR"])
def test_partial_application_preserves_both_original_quantities(drafts, direction, asset):
    s = drafts
    body, group = prepared(s, direction=direction, asset=asset, initial=False)
    pay(s, body)
    reads = BusinessSettlementBalances(s["engine"])
    source = group.lines[0]
    current = reads.document_capacity(s["owner"], s["ledger"], source.document_id)
    assert (current.total_amount, current.applied_amount, current.outstanding_amount) == (
        "100.00",
        "0.00",
        "100.00",
    )
    used = "44" if asset == "EUR" else "40"
    part = group.model_copy(
        update={
            "lines": [source.model_copy(update={"payment_amount": used, "document_amount": "40"})]
        }
    )
    allocate(s, part)
    before = snapshot(s)
    (row,) = reads.list_payments(s["owner"], s["ledger"])
    assert row.id == body.id and row.asset_id == "USD" and row.direction == direction
    assert row.applied_amount == used + ".00" and row.fee_count == 1
    assert row.unapplied_amount == ("66.00" if asset == "EUR" else "60.00")
    (allocation,) = reads.list_allocations(s["owner"], s["ledger"], payment_id=body.id)
    assert allocation.payment_amount == used + ".00" and not allocation.is_initial
    assert allocation.line_count == 1 and allocation.id == part.id
    current = reads.document_capacity(s["owner"], s["ledger"], source.document_id)
    assert current.asset_id == asset
    assert (current.applied_amount, current.outstanding_amount) == ("40.00", "60.00")
    assert current.lines[0].project_id == s["project"].id
    assert snapshot(s) == before


def test_pages_batch_sums_without_loading_receipts_or_fee_json(drafts):
    s = drafts
    body, group = prepared(s)
    pay(s, body)
    second = body.model_copy(update={"id": uuid4(), "initial_allocation": None})
    pay(s, second, "fictional-second")
    reads = BusinessSettlementBalances(s["engine"])
    for limit in (1, 2):
        statements = []

        def record(
            conn, cursor, statement, parameters, context, executemany, statements=statements
        ):
            statements.append(statement)

        event.listen(s["engine"], "before_cursor_execute", record)
        try:
            rows = reads.list_payments(s["owner"], s["ledger"], limit=limit)
        finally:
            event.remove(s["engine"], "before_cursor_execute", record)
        assert len(rows) == limit
        assert len([sql for sql in statements if sql.startswith("SELECT")]) == 5
        assert not any("command_receipts" in sql or "pg_advisory" in sql for sql in statements)
        assert any("READ ONLY" in sql for sql in statements)
        assert not any("business_payments.fees AS" in sql for sql in statements)
    assert reads.list_payments(s["owner"], s["ledger"], limit=1, offset=1)[0].id == second.id
    assert reads.list_payments(s["owner"], s["ledger"], offset=2) == []
    (allocation,) = reads.list_allocations(s["owner"], s["ledger"])
    assert allocation.is_initial and allocation.id == group.id
    assert reads.list_allocations(s["owner"], s["ledger"], payment_id=second.id) == []


def test_reads_keep_a_single_snapshot_during_a_concurrent_allocation(drafts):
    s = drafts
    body, group = prepared(s, initial=False)
    pay(s, body)
    reads = BusinessSettlementBalances(s["engine"])
    committed = False

    def record(conn, cursor, statement, parameters, context, executemany):
        nonlocal committed
        if not committed and statement.startswith("SELECT") and "business_payments" in statement:
            committed = True
            allocate(s, group)

    event.listen(s["engine"], "before_cursor_execute", record)
    try:
        rows = reads.list_payments(s["owner"], s["ledger"])
    finally:
        event.remove(s["engine"], "before_cursor_execute", record)
    assert committed and rows[0].unapplied_amount == "100.00"
    assert reads.list_payments(s["owner"], s["ledger"])[0].unapplied_amount == "0.00"


def test_scoped_archived_and_disabled_history_stays_readable(drafts):
    s = drafts
    body, group = prepared(s)
    pay(s, body)
    reads = BusinessSettlementBalances(s["engine"])
    other = s["structure"].create_entity(
        s["owner"],
        EntityCreate(
            kind="personal",
            name="Fictional other settlement",
            base_asset_id="USD",
            template_key="personal_default",
        ),
    )
    assert reads.list_payments(s["owner"], other.ledger.id) == []
    assert reads.list_allocations(s["owner"], other.ledger.id) == []
    with pytest.raises(LedgerError) as error:
        reads.document_capacity(s["owner"], other.ledger.id, group.lines[0].document_id)
    assert error.value.status == 404
    for method in (reads.list_payments, reads.list_allocations):
        with pytest.raises(LedgerError) as error:
            method(uuid4(), s["ledger"])
        assert error.value.status == 404
    s["structure"].update_asset(s["owner"], "USD", AssetUpdate(expected_version=1, enabled=False))
    s["structure"].update_entity(
        s["owner"], s["entity"].id, EntityUpdate(expected_version=1, archived=True)
    )
    before = snapshot(s)
    assert reads.list_payments(s["owner"], s["ledger"])[0].amount == "100.00"
    assert reads.list_allocations(s["owner"], s["ledger"])[0].payment_amount == "100.00"
    assert (
        reads.document_capacity(
            s["owner"], s["ledger"], group.lines[0].document_id
        ).outstanding_amount
        == "0.00"
    )
    assert snapshot(s) == before


@pytest.mark.parametrize("page", [dict(limit=0), dict(limit=201), dict(offset=-1)])
def test_invalid_pages_are_rejected(drafts, page):
    s = drafts
    reads = BusinessSettlementBalances(s["engine"])
    for method in (reads.list_payments, reads.list_allocations):
        with pytest.raises(LedgerError):
            method(s["owner"], s["ledger"], **page)
