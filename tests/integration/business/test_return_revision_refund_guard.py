"""A return can never silently detach an effective refund, even when principal is available."""

from concurrent.futures import ThreadPoolExecutor
from datetime import date
from decimal import Decimal
from threading import Event, get_ident
from time import monotonic, sleep
from uuid import uuid4

import pytest
from coinpup_api.business.allocation_return_revision_models import (
    BusinessAllocationReturnRevision as Revision,
)
from coinpup_api.business.allocation_return_schemas import BusinessAllocationReturnCreate
from coinpup_api.business.refund_schemas import BusinessRefundCreate
from coinpup_api.business.settlement_models import BusinessAllocationLine
from coinpup_api.business.settlement_totals import payment_totals
from sqlalchemy import event, insert, select, text
from sqlalchemy.exc import IntegrityError

from tests.integration.business.test_allocation_return_service import submit_return
from tests.integration.business.test_draft_service import drafts as drafts
from tests.integration.business.test_refund_revision_service import correction, submit
from tests.integration.business.test_refund_service import refund
from tests.integration.business.test_settlement_service import pay, prepared
from tests.integration.ledger.test_account_classes import snapshot
from tests.integration.ledger.test_posting_service_database import ledger_setup as ledger_setup

pytestmark = pytest.mark.integration


def example(s, direction="payout", *, linked=True):
    payment, allocation = prepared(s, direction=direction)
    small = payment.initial_allocation.lines[0].model_copy(
        update={"payment_amount": "20", "document_amount": "20"}
    )
    pay(
        s,
        payment.model_copy(
            update={
                "initial_allocation": payment.initial_allocation.model_copy(
                    update={"lines": [small]}
                )
            }
        ),
    )
    with s["engine"].connect() as c:
        source = c.scalar(
            select(BusinessAllocationLine.id).where(
                BusinessAllocationLine.allocation_id == allocation.id
            )
        )
    body = BusinessAllocationReturnCreate(
        id=uuid4(),
        payment_id=payment.id,
        return_date="2026-10-01",
        reason="Fictional full return",
        lines=[
            dict(
                source_allocation_line_id=source,
                expected_version=1,
                payment_amount="20",
                document_amount="20",
            )
        ],
    )
    returned = submit_return(s, body)
    cash = BusinessRefundCreate(
        id=uuid4(),
        payment_id=payment.id,
        expected_version=1,
        account_id=payment.account_id,
        asset_id="USD",
        amount="5",
        payment_amount="5",
        transaction_date="2026-10-02",
        reason="Fictional actual cash",
        allocation_return_ids=[returned.id] if linked else [],
    )
    return body, returned, cash, refund(s, cash)


def probe(s, returned, *, on=None, waiting=None):
    data = dict(
        id=uuid4(),
        ledger_id=s["ledger"],
        return_id=returned.id,
        payment_id=returned.payment_id,
        created_by=s["owner"],
        idempotency_key="fictional-return",
        group_version=2,
        previous_revision_id=None,
        action="correct" if on else "cancel",
        revision_reason="Fictional explicit revision",
        return_date=on,
        return_reason="Fictional moved date" if on else None,
    )
    with pytest.raises(IntegrityError) as caught, s["engine"].begin() as c:
        if waiting:
            waiting[0].append(c.connection.driver_connection.info.backend_pid)
            waiting[1].set()
        c.execute(insert(Revision).values(**data))
        c.exec_driver_sql("SET CONSTRAINTS tr_business_return_revisions_complete IMMEDIATE")
    return caught.value.orig.diag.constraint_name


@pytest.mark.parametrize("direction", ["receipt", "payout"])
def test_locks_current_evidence_through_explicit_changes_and_preserves_all_old_receipts(
    drafts, direction
):
    s = drafts
    body, returned, cash, first = example(s, direction)
    with s["engine"].connect() as c:
        applied, refunded = payment_totals(c, s["ledger"], [returned.payment_id])[
            returned.payment_id
        ]
        assert (applied, refunded) == (Decimal("0"), Decimal("5"))
        assert Decimal("100") - Decimal("20") - refunded == Decimal("75")
    second_body = cash.model_copy(update={"id": uuid4(), "transaction_date": date(2026, 10, 5)})
    second = refund(s, second_body, "fictional-later-refund")
    before = snapshot(s)
    assert probe(s, returned) == "ck_return_revision_refund_reference"
    assert probe(s, returned, on=date(2026, 10, 3)) == "ck_return_revision_refund_date"
    assert probe(s, returned, on=date(2026, 10, 2)) == "ck_return_revision_shape"
    assert snapshot(s) == before
    detach = correction(s, first, amount="5", payment_amount="5", allocation_return_ids=[])
    detached = submit(s, first, "correct", detach, "fictional-detach-one")
    assert probe(s, returned) == "ck_return_revision_refund_reference"
    assert probe(s, returned, on=date(2026, 10, 5)) == "ck_return_revision_shape"
    assert probe(s, returned, on=date(2026, 10, 6)) == "ck_return_revision_refund_date"
    cancel = {"expected_version": 1, "reason": "Fictional cancelled cash"}
    stopped = submit(s, second, "cancel", cancel, "fictional-stop-refund")
    before = snapshot(s)
    # The separate member-validation gate still rejects every incomplete group.
    assert probe(s, returned) == "ck_return_revision_shape"
    assert submit_return(s, body) == returned and refund(s, cash) == first
    assert refund(s, second_body, "fictional-later-refund") == second
    assert submit(s, first, "correct", detach, "fictional-detach-one") == detached
    assert submit(s, second, "cancel", cancel, "fictional-stop-refund") == stopped
    assert snapshot(s) == before


@pytest.mark.parametrize("previously_linked", [False, True])
def test_competing_refund_commit_changes_the_locked_decision_for_the_waiting_return(
    drafts, previously_linked
):
    s = drafts
    _, returned, _, original = example(s, linked=previously_linked)
    change = correction(
        s,
        original,
        amount="5",
        payment_amount="5",
        allocation_return_ids=[] if previously_linked else [str(returned.id)],
    )
    held, release, waiting = Event(), Event(), Event()
    writer, blocked_pid = [], []

    def before_commit(c):
        if writer and writer[0] == get_ident():
            held.set()
            assert release.wait(15), "test did not release committing writer"

    def change_refund():
        writer.append(get_ident())
        return submit(s, original, "correct", change, "fictional-competing-refund")

    engine = s["engine"]
    event.listen(engine, "commit", before_commit)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            cash = pool.submit(change_refund)
            try:
                assert held.wait(10)
                returning = pool.submit(probe, s, returned, waiting=(blocked_pid, waiting))
                assert waiting.wait(10)
                deadline = monotonic() + 7
                with engine.connect() as observer:
                    while not observer.scalar(
                        text("SELECT cardinality(pg_blocking_pids(:p))"), {"p": blocked_pid[0]}
                    ):
                        assert monotonic() < deadline, (
                            "return did not wait for the actual refund write lock"
                        )
                        sleep(0.01)
            finally:
                release.set()
            assert cash.result(timeout=10).version == 2
            constraint = returning.result(timeout=10)
    finally:
        event.remove(engine, "commit", before_commit)
    assert constraint == (
        "ck_return_revision_shape" if previously_linked else "ck_return_revision_refund_reference"
    )
