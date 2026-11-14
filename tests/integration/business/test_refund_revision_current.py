"""Current command inputs are ledger-scoped and always retain the last actual sealed posting."""

from uuid import uuid4

import pytest
from coinpup_api.business.refund_revision_current import read_current_refund
from coinpup_api.ledger.models import FinancialOperation
from coinpup_api.ledger.service import LedgerError
from sqlalchemy import event, update
from sqlalchemy.orm import Session

from tests.integration.business.test_draft_service import drafts as drafts
from tests.integration.business.test_refund_revision_lifecycle import advance
from tests.integration.business.test_refund_service import refund, setup_refund
from tests.integration.ledger.test_account_classes import snapshot
from tests.integration.ledger.test_posting_service_database import ledger_setup as ledger_setup

pytestmark = pytest.mark.integration


def read(s, identifier):
    selects = []
    with s["engine"].connect() as c, Session(bind=c) as session:
        c.exec_driver_sql("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        event.listen(
            c,
            "before_cursor_execute",
            lambda conn, cursor, statement, *args: selects.append(statement),
        )
        value = read_current_refund(session, s["ledger"], identifier)
        assert len(selects) == 4 and all(
            sql.lstrip().upper().startswith("SELECT") for sql in selects
        )
        return value


@pytest.mark.parametrize("asset,amount,legs", [("USD", "20", 2), ("EUR", "19.37", 4)])
def test_four_reads_keep_each_current_and_cancelled_posting_and_its_evidence(
    drafts, asset, amount, legs
):
    s = drafts
    _, _, body = setup_refund(s, asset=asset, amount=amount)
    original = refund(s, body)
    before = snapshot(s)
    first = read(s, original.id)
    assert first.refund.id == original.id and first.payment.id == original.payment_id
    assert first.payment_operation.current_journal_id == first.payment.journal_id
    assert (
        first.operation.version == 1
        and first.revision is None
        and first.journal.id == original.journal_id
    )
    assert (
        first.fact.amount == original.amount
        and first.fact.payment_amount == original.payment_amount
    )
    assert first.fact.allocation_return_ids == original.allocation_return_ids
    assert [line.line_no for line in first.lines] == list(range(1, legs + 1))
    assert snapshot(s) == before
    with s["engine"].begin() as c:
        corrected, fixed = advance(c, s, original, "correct")
    v2 = read(s, original.id)
    assert (
        v2.revision.id == corrected["id"] and v2.journal.id == corrected["replacement_journal_id"]
    )
    assert (
        v2.fact.model_dump(mode="json") == fixed["replacement"]
        and v2.fact.allocation_return_ids == ()
    )
    with s["engine"].begin() as c:
        terminal, _ = advance(c, s, original, "cancel")
    before = snapshot(s)
    v3 = read(s, original.id)
    assert (
        v3.operation.version == 3
        and v3.operation.status == "cancelled"
        and v3.revision.id == terminal["id"]
    )
    assert v3.journal.id == v2.journal.id and v3.fact == v2.fact
    assert [line.id for line in v3.lines] == [line.id for line in v2.lines]
    assert refund(s, body) == original and snapshot(s) == before


@pytest.mark.parametrize("bad", ["unknown", "foreign", "isolated_head", "missing_journal"])
def test_lookup_distinguishes_out_of_scope_from_corrupt_in_scope_operation(drafts, bad):
    s = drafts
    _, _, body = setup_refund(s, allocated=False)
    original = refund(s, body)
    before = snapshot(s)
    with s["engine"].connect() as c, Session(bind=c) as session:
        if bad == "isolated_head":
            c.execute(
                update(FinancialOperation)
                .where(FinancialOperation.id == original.operation_id)
                .values(version=2, status="cancelled")
            )
        if bad == "missing_journal":
            c.execute(
                update(FinancialOperation)
                .where(FinancialOperation.id == original.operation_id)
                .values(version=2, current_journal_id=uuid4())
            )
        with pytest.raises(LedgerError) as error:
            read_current_refund(
                session,
                uuid4() if bad == "foreign" else s["ledger"],
                uuid4() if bad == "unknown" else original.id,
            )
        assert (error.value.status, error.value.code) == (
            (404, "not_found")
            if bad in {"unknown", "foreign"}
            else (503, "refund_revision_integrity")
        )
    assert snapshot(s) == before
