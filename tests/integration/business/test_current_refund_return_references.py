"""Only explicitly retained current evidence constrains a fictional allocation return."""

from uuid import uuid4

import pytest
from coinpup_api.business.refund_schemas import BusinessRefundCreate
from sqlalchemy import text

from tests.integration.business.test_allocation_return_service import setup_return, submit_return
from tests.integration.business.test_draft_service import drafts as drafts
from tests.integration.business.test_refund_revision_service import correction, submit
from tests.integration.business.test_refund_service import refund, setup_refund
from tests.integration.ledger.test_account_classes import snapshot
from tests.integration.ledger.test_posting_service_database import ledger_setup as ledger_setup

pytestmark = pytest.mark.integration


def references(s, payment, ids, *, ledger=None):
    with s["engine"].connect() as conn:
        return conn.execute(
            text(
                "SELECT return_id, return_version, refund_id, refund_version, revision_id, "
                "line_no, refund_date FROM coinpup_current_refund_return_references("
                ":ledger, :payment, CAST(:ids AS uuid[])) ORDER BY refund_id, line_no"
            ),
            {"ledger": ledger or s["ledger"], "payment": payment, "ids": ids},
        ).all()


@pytest.mark.parametrize("direction", ["receipt", "payout"])
def test_each_active_refund_must_explicitly_release_evidence_and_preserve_prior_receipts(
    drafts, direction
):
    s = drafts
    payment, _, base = setup_refund(s, direction)
    first_body = base.model_copy(update={"amount": "5", "payment_amount": "5"})
    second_body = first_body.model_copy(update={"id": uuid4()})
    first = refund(s, first_body)
    second = refund(s, second_body, "fictional-secondary-refund")
    target = first.allocation_return_ids[0]
    assert set(references(s, payment.id, [target, target, uuid4()])) == {
        (target, 1, item.id, 1, None, 1, item.transaction_date) for item in (first, second)
    }
    assert not references(s, uuid4(), [target])
    assert not references(s, payment.id, [target], ledger=uuid4())
    assert not references(s, payment.id, [])
    assert not references(s, payment.id, [uuid4()])

    detach = correction(s, first, amount="5", payment_amount="5")
    v2 = submit(s, first, "correct", detach, "fictional-explicit-detach")
    assert references(s, payment.id, [target]) == [
        (target, 1, second.id, 1, None, 1, second.transaction_date)
    ]
    attach = correction(
        s, first, 2, amount="5", payment_amount="5", allocation_return_ids=[str(target)]
    )
    v3 = submit(s, first, "correct", attach, "fictional-explicit-reattach")
    assert set(references(s, payment.id, [target])) == {
        (target, 1, second.id, 1, None, 1, second.transaction_date),
        (target, 1, first.id, 3, v3.revision_id, 1, v3.replacement.transaction_date),
    }
    end = {"expected_version": 3, "reason": "Fictional terminal refund"}
    v4 = submit(s, first, "cancel", end, "fictional-end-one")
    assert [r.refund_id for r in references(s, payment.id, [target])] == [second.id]
    second_v2 = submit(s, second, "correct", detach, "fictional-release-last")
    before = snapshot(s)
    assert references(s, payment.id, [target]) == []
    assert refund(s, first_body) == first
    assert refund(s, second_body, "fictional-secondary-refund") == second
    assert submit(s, first, "correct", detach, "fictional-explicit-detach") == v2
    assert submit(s, first, "correct", attach, "fictional-explicit-reattach") == v3
    assert submit(s, first, "cancel", end, "fictional-end-one") == v4
    assert submit(s, second, "correct", detach, "fictional-release-last") == second_v2
    assert snapshot(s) == before


def test_revision_only_evidence_uses_its_own_date_position_and_bound_source_version(drafts):
    s = drafts
    payment, _, _, body = setup_return(s)
    first = submit_return(s, body)
    second = submit_return(s, body.model_copy(update={"id": uuid4()}), "fictional-other-return")
    created = BusinessRefundCreate(
        id=uuid4(),
        payment_id=payment.id,
        expected_version=1,
        account_id=payment.account_id,
        asset_id="USD",
        amount="5",
        payment_amount="5",
        transaction_date="2026-10-02",
        reason="Fictional one original reference",
        allocation_return_ids=[first.id],
    )
    original = refund(s, created)
    corrected = correction(s, original, allocation_return_ids=[str(second.id), str(first.id)])
    v2 = submit(s, original, "correct", corrected, "fictional-order-two-returns")
    assert references(s, payment.id, [first.id, second.id, first.id]) == [
        (second.id, 1, original.id, 2, v2.revision_id, 1, v2.replacement.transaction_date),
        (first.id, 1, original.id, 2, v2.revision_id, 2, v2.replacement.transaction_date),
    ]
    assert [row.line_no for row in references(s, payment.id, [first.id])] == [2]
    assert [row.return_id for row in references(s, payment.id, [second.id])] == [second.id]
