"""Two PostgreSQL connections compete for fixed payment principal, versions and periods."""

from decimal import Decimal, Inexact, Rounded, localcontext
from uuid import uuid4

import pytest
from coinpup_api.business.refund_schemas import BusinessRefundCreate
from coinpup_api.business.settlement_schemas import BusinessAllocationCreate
from coinpup_api.business.settlement_totals import payment_totals
from coinpup_api.business.settlements import BusinessSettlementService
from coinpup_api.ledger.models import CommandReceipt
from coinpup_api.ledger.service import LedgerError
from sqlalchemy import func, select

from tests.integration.business.test_allocation_return_service import setup_return, submit_return
from tests.integration.business.test_credit_revision_boundaries import race
from tests.integration.business.test_draft_service import drafts as drafts
from tests.integration.business.test_refund_revision_service import correction, submit
from tests.integration.business.test_refund_service import refund, setup_refund
from tests.integration.ledger.test_account_classes import snapshot
from tests.integration.ledger.test_period_transactions import change as change_period
from tests.integration.ledger.test_posting_service_database import ledger_setup as ledger_setup
from tests.integration.ledger.test_posting_service_database import opening

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("direction", ["receipt", "payout"])
@pytest.mark.parametrize("competitor", ["refund", "allocation"])
def test_locked_revision_and_other_payment_consumers_see_one_fresh_principal(
    drafts, direction, competitor
):
    s = drafts
    payment, _, allocation, release = setup_return(s, direction=direction)
    returned = submit_return(s, release)
    body = BusinessRefundCreate(
        id=uuid4(),
        payment_id=payment.id,
        expected_version=1,
        account_id=payment.account_id,
        asset_id="USD",
        amount="10.00",
        payment_amount="10.00",
        transaction_date="2026-10-02",
        reason="Fictional principal contention",
        allocation_return_ids=[returned.id],
    )
    original = refund(s, body)
    request = correction(s, original)
    if competitor == "refund":
        second = body.model_copy(update={"id": uuid4()})

        def compete():
            return refund(s, second, key="fictional-competing-refund")
    else:
        extra = BusinessAllocationCreate.model_validate(
            allocation.model_dump()
            | dict(
                id=uuid4(),
                allocation_date="2026-10-02",
                lines=[
                    allocation.lines[0].model_dump()
                    | dict(document_amount="10", payment_amount="10")
                ],
            )
        )

        def compete():
            return BusinessSettlementService(s["engine"]).allocate(
                s["owner"], s["ledger"], extra, "fictional-competing-allocation"
            )

    revised, other = race(
        s,
        [lambda: submit(s, original, "correct", request, "fictional-competing-revision"), compete],
    )
    won = not isinstance(revised, LedgerError)
    assert won == isinstance(other, LedgerError)
    failure = other if won else revised
    assert failure.code == (
        "allocation_exceeds_payment"
        if won and competitor == "allocation"
        else "refund_exceeds_payment"
    )
    with s["engine"].connect() as c:
        amounts = payment_totals(c, s["ledger"], [payment.id])[payment.id]
        expect = (80, 15) if won else ((80, 20) if competitor == "refund" else (90, 10))
        assert amounts == tuple(map(Decimal, expect))
    before = snapshot(s)
    if won:
        assert submit(s, original, "correct", request, "fictional-competing-revision") == revised
    assert refund(s, body) == original and snapshot(s) == before


@pytest.mark.parametrize("mode", ["same_intent", "different_intent", "different_key"])
def test_exact_key_races_bind_raw_intent_before_current_version_validation(drafts, mode):
    s = drafts
    _, _, body = setup_refund(s)
    original = refund(s, body)
    one = correction(s, original)
    two = one if mode != "different_intent" else one | {"reason": "Fictional competing reason"}
    key1 = "fictional-two-connections"
    key2 = "fictional-other-key" if mode == "different_key" else key1
    outcomes = race(
        s,
        [
            lambda: submit(s, original, "correct", one, key1),
            lambda: submit(s, original, "correct", two, key2),
        ],
    )
    if mode == "same_intent":
        assert outcomes[0] == outcomes[1] and outcomes[0].version == 2
    else:
        errors = [r for r in outcomes if isinstance(r, LedgerError)]
        assert len(errors) == 1 and errors[0].code == (
            "version_conflict" if mode == "different_key" else "idempotency_conflict"
        )
    with s["engine"].connect() as c:
        assert (
            c.scalar(
                select(func.count())
                .select_from(CommandReceipt)
                .where(
                    CommandReceipt.operation_id == original.operation_id,
                    CommandReceipt.response_status == 200,
                )
            )
            == 1
        )


@pytest.mark.parametrize("action", ["correct", "cancel"])
def test_period_close_and_refund_revision_serialize_before_any_new_fact(drafts, action):
    s = drafts
    _, _, body = setup_refund(s)
    original = refund(s, body)
    cmd = (
        correction(s, original)
        if action == "correct"
        else {"expected_version": 1, "reason": "Fictional period race"}
    )
    result, closed = race(
        s,
        [
            lambda: submit(s, original, action, cmd, "fictional-close-race"),
            lambda: change_period(s, cutoff="2026-10-02"),
        ],
    )
    assert not isinstance(closed, LedgerError)
    before = snapshot(s)
    if isinstance(result, LedgerError):
        assert result.code == "period_closed"
        with pytest.raises(LedgerError) as error:
            submit(s, original, action, cmd, "fictional-close-race")
        assert error.value.code == "period_closed"
    else:
        assert submit(s, original, action, cmd, "fictional-close-race") == result
    assert refund(s, body) == original and snapshot(s) == before


@pytest.mark.parametrize("asset,scale", [("BTC", 8), ("ETH", 18)])
def test_minimum_and_maximum_cross_asset_cash_never_depend_on_decimal_context(drafts, asset, scale):
    s = drafts
    _, _, body = setup_refund(s, direction="receipt", allocated=False)
    original = refund(s, body)
    maximum = "9" * 20 + "." + "9" * scale
    minimum = "0." + "0" * (scale - 1) + "1"
    s["posting"].post_opening(
        s["owner"], s["ledger"], opening(s, maximum, asset), "fictional-exact-large-opening"
    )
    high = correction(s, original, asset_id=asset, amount=maximum, payment_amount="0.01")
    low = correction(s, original, 2, asset_id=asset, amount=minimum, payment_amount="0.01")
    with localcontext() as ctx:
        ctx.prec = 2
        ctx.traps[Inexact] = True
        ctx.traps[Rounded] = True
        first = submit(s, original, "correct", high, "fictional-exact-high")
        second = submit(s, original, "correct", low, "fictional-exact-low")
        assert Decimal(first.replacement.amount) == Decimal(maximum)
        assert second.previous == first.replacement and Decimal(
            second.replacement.amount
        ) == Decimal(minimum)
        before = snapshot(s)
        assert submit(s, original, "correct", high, "fictional-exact-high") == first
        assert snapshot(s) == before
