"""An earlier return cannot consume capacity now owned by other real financial commands."""

from uuid import uuid4

import pytest
from coinpup_api.business.credit_schemas import BusinessCreditCreate
from coinpup_api.business.models import BusinessDocument
from coinpup_api.business.refund_schemas import BusinessRefundCreate
from coinpup_api.ledger.schemas import EntityCreate
from coinpup_api.ledger.service import LedgerError
from sqlalchemy import select

from tests.integration.business.test_allocation_return_service import setup_return, submit_return
from tests.integration.business.test_credit_service import credit
from tests.integration.business.test_draft_service import drafts as drafts
from tests.integration.business.test_refund_revision_service import correction, submit
from tests.integration.business.test_refund_service import refund
from tests.integration.business.test_return_revision_service import cancel, command, run
from tests.integration.business.test_settlement_service import pay
from tests.integration.ledger.test_account_classes import snapshot
from tests.integration.ledger.test_period_transactions import change as change_period
from tests.integration.ledger.test_posting_service_database import ledger_setup as ledger_setup

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("consumer", ["refund", "credit", "other-payment"])
@pytest.mark.parametrize("direction", ["receipt", "payout"])
def test_one_cent_capacity_competition_and_previous_command_preservation(
    drafts, consumer, direction
):
    s = drafts
    payment, _, _, body = setup_return(s, direction=direction)
    original = submit_return(s, body)
    member = original.postings[0]
    line = body.lines[0]
    decreased = line.model_copy(update={"document_amount": "19.99", "payment_amount": "19.99"})
    intent = command(body, lines=[decreased])
    if consumer == "refund":
        cash = BusinessRefundCreate(
            id=uuid4(),
            payment_id=original.payment_id,
            expected_version=1,
            account_id=payment.account_id,
            asset_id="USD",
            amount="20",
            payment_amount="20",
            transaction_date="2026-10-06",
            reason="Fictional independent cash",
            allocation_return_ids=[],
        )
        issued = refund(s, cash)
    elif consumer == "credit":
        with s["engine"].connect() as conn:
            version = conn.scalar(
                select(BusinessDocument.version).where(BusinessDocument.id == member.document_id)
            )
        credit(
            s,
            BusinessCreditCreate(
                id=uuid4(),
                document_id=member.document_id,
                expected_version=version,
                credit_date="2026-10-06",
                reason="Fictional independent credit",
                lines=[
                    dict(
                        source_line_id=member.document_line_id,
                        net_amount="20",
                        tax_amount="0",
                        recognition_date="2026-10-06",
                    )
                ],
            ),
        )
    else:
        template = payment.initial_allocation
        extra = template.model_copy(
            update={
                "id": uuid4(),
                "lines": [
                    template.lines[0].model_copy(
                        update={"payment_amount": "20", "document_amount": "20"}
                    )
                ],
            }
        )
        pay(
            s,
            payment.model_copy(update={"id": uuid4(), "amount": "20", "initial_allocation": extra}),
            "fictional-second-payment",
        )
    before = snapshot(s)
    with pytest.raises(LedgerError) as rejected:
        run(s, original.id, intent)
    expected = (
        "allocation_return_payment_capacity"
        if consumer == "refund"
        else "allocation_return_document_capacity"
    )
    assert (rejected.value.status, rejected.value.code) == (409, expected)
    assert submit_return(s, body) == original and snapshot(s) == before
    if consumer == "refund":
        submit(
            s,
            issued,
            "correct",
            correction(s, issued, amount="19.99", payment_amount="19.99"),
            "fictional-release-cent",
        )
        accepted = run(s, original.id, intent)
        assert accepted.version == 2 and refund(s, cash) == issued


@pytest.mark.parametrize(
    "guard", ["source-date", "closed-original", "foreign-group", "foreign-source", "precision"]
)
def test_rejected_scopes_periods_and_amounts_leave_all_tables_unchanged(drafts, guard):
    s = drafts
    _, _, _, body = setup_return(s, asset="EUR")
    original = submit_return(s, body)
    line = body.lines[0]
    target = original.id
    intent = command(body)
    if guard == "source-date":
        intent = command(body, day="2026-09-29")
        code = "allocation_return_date"
    elif guard == "closed-original":
        change_period(s, cutoff="2026-10-01")
        code = "period_closed"
    elif guard == "foreign-group":
        other = s["structure"].create_entity(
            s["owner"],
            EntityCreate(
                kind="personal",
                name="Fictional other ledger",
                base_asset_id="USD",
                template_key="personal_default",
            ),
        )
        s = dict(s, ledger=other.ledger.id)
        intent = cancel(1)
        code = "not_found"
    elif guard == "foreign-source":
        intent = command(
            body, lines=[line.model_copy(update={"source_allocation_line_id": uuid4()})]
        )
        code = "not_found"
    else:
        intent = command(body, lines=[line.model_copy(update={"document_amount": "20.001"})])
        code = "amount_precision"
    before = snapshot(s)
    with pytest.raises(LedgerError) as rejected:
        run(s, target, intent)
    assert rejected.value.code == code and snapshot(s) == before
