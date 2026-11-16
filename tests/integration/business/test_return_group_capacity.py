"""Direct return-side capacity proof for money already consumed by later commands."""

import runpy
from datetime import date
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from coinpup_api.business.allocation_return_revision_member_models import (
    BusinessReturnRevisionMember as Member,
)
from coinpup_api.business.allocation_return_revision_models import (
    BusinessAllocationReturnRevision as Revision,
)
from coinpup_api.business.credit_schemas import BusinessCreditCreate
from coinpup_api.business.models import BusinessDocument
from coinpup_api.business.refund_schemas import BusinessRefundCreate
from sqlalchemy import insert, select, text
from sqlalchemy.exc import IntegrityError

from tests.integration.business.test_allocation_return_revision_headers import revision as revision
from tests.integration.business.test_allocation_return_revision_members import member
from tests.integration.business.test_allocation_return_service import setup_return, submit_return
from tests.integration.business.test_credit_revision_storage import definitions
from tests.integration.business.test_credit_service import credit
from tests.integration.business.test_draft_service import drafts as drafts
from tests.integration.business.test_refund_revision_service import correction, submit
from tests.integration.business.test_refund_service import refund
from tests.integration.business.test_settlement_service import pay
from tests.integration.ledger.test_account_classes import snapshot
from tests.integration.ledger.test_posting_service_database import ledger_setup as ledger_setup

pytestmark = pytest.mark.integration


def validate(c, ledger, payment, sources):
    c.execute(
        text("SELECT coinpup_validate_return_capacity(:ledger,:payment,CAST(:sources AS uuid[]))"),
        dict(ledger=ledger, payment=payment, sources=sources),
    )


def revise(c, h, orig, quantity):
    c.execute(insert(Revision).values(**h))
    c.execute(
        insert(Member).values(
            **(
                member(h, orig)
                | {"payment_amount": Decimal(quantity), "document_amount": Decimal(quantity)}
            )
        )
    )


@pytest.mark.parametrize("spending", ["refund", "credit"])
def test_one_cent_over_a_committed_credit_or_refund_rejects_the_return(revision, spending):
    s, h, body, orig = revision
    f = orig.postings[0]
    if spending == "refund":
        cash = BusinessRefundCreate(
            id=uuid4(),
            payment_id=orig.payment_id,
            expected_version=1,
            account_id=s["account"].id,
            asset_id="USD",
            amount="20",
            payment_amount="20",
            transaction_date="2026-10-06",
            reason="Fictional independent principal",
            allocation_return_ids=[],
        )
        receipt = refund(s, cash)
    else:
        with s["engine"].connect() as c:
            version = c.scalar(
                select(BusinessDocument.version).where(BusinessDocument.id == f.document_id)
            )
        credit(
            s,
            BusinessCreditCreate(
                id=uuid4(),
                document_id=f.document_id,
                expected_version=version,
                credit_date="2026-10-06",
                reason="Fictional released line capacity",
                lines=[
                    dict(
                        source_line_id=f.document_line_id,
                        net_amount="20",
                        tax_amount="0",
                        recognition_date="2026-10-06",
                    )
                ],
            ),
        )
    args = (s["ledger"], orig.payment_id, [f.source_allocation_line_id])
    before = snapshot(s)
    expected = (
        "ck_return_revision_payment_capacity"
        if spending == "refund"
        else "ck_return_revision_document_capacity"
    )
    with s["engine"].connect() as c:
        validate(c, *args)
        revise(c, h, orig, "19.99")
        with pytest.raises(IntegrityError) as caught:
            validate(c, *args)
        assert caught.value.orig.diag.constraint_name == expected
        c.rollback()
    assert submit_return(s, body) == orig and snapshot(s) == before
    if spending == "refund":
        # The original 20 still exists, but after an explicit refund correction only 19.99
        # consumes principal. The next cent of free capacity is real.
        result = submit(
            s,
            receipt,
            "correct",
            correction(s, receipt, amount="19.99", payment_amount="19.99"),
            "fictional-reduce",
        )
        assert Decimal(result.replacement.payment_amount) == Decimal("19.99")
        before = snapshot(s)
        with s["engine"].connect() as c:
            revise(c, h, orig, "19.99")
            validate(c, *args)
            c.rollback()
        assert snapshot(s) == before


@pytest.mark.parametrize("direction,asset", [("receipt", "USD"), ("payout", "EUR")])
def test_other_payments_on_the_same_document_line_consume_the_released_document_asset(
    drafts, direction, asset
):
    s = drafts
    payment, _, _, body = setup_return(s, direction=direction, asset=asset)
    original = submit_return(s, body)
    first = original.postings[0]
    # Consume the returned 20 in the document's asset with a separate real payment.
    template = payment.initial_allocation
    p = "22" if asset == "EUR" else "20"
    allocation = template.model_copy(
        update={
            "id": uuid4(),
            "lines": [
                template.lines[0].model_copy(update={"document_amount": "20", "payment_amount": p})
            ],
        }
    )
    pay(
        s,
        payment.model_copy(update={"id": uuid4(), "amount": p, "initial_allocation": allocation}),
        "fictional-second-payment",
    )
    h = dict(
        id=uuid4(),
        return_id=original.id,
        payment_id=original.payment_id,
        ledger_id=s["ledger"],
        group_version=2,
        previous_revision_id=None,
        action="correct",
        revision_reason="Fictional shrink",
        return_date=date(2026, 11, 8),
        return_reason="Fictional other payment",
        created_by=s["owner"],
        idempotency_key="fictional-return-shrink",
    )
    before = snapshot(s)
    with s["engine"].connect() as c:
        args = (s["ledger"], original.payment_id, [first.source_allocation_line_id])
        validate(c, *args)
        c.execute(insert(Revision).values(**h))
        c.execute(
            insert(Member).values(
                **(
                    member(h, original)
                    | dict(
                        payment_amount=Decimal("11" if asset == "EUR" else "10"),
                        document_amount=Decimal("10"),
                    )
                )
            )
        )
        with pytest.raises(IntegrityError) as caught:
            validate(c, *args)
        assert caught.value.orig.diag.constraint_name == "ck_return_revision_document_capacity"
        c.rollback()
    assert snapshot(s) == before


@pytest.mark.parametrize("fault", ["ledger", "payment", "source", "empty", "partial"])
def test_every_requested_source_must_belong_to_the_ledger_and_payment(revision, fault):
    s, _, _, original = revision
    line = original.postings[0].source_allocation_line_id
    sources = {"source": [uuid4()], "empty": [], "partial": [line, uuid4()]}.get(fault, [line])
    with pytest.raises(IntegrityError) as caught, s["engine"].begin() as c:
        validate(
            c,
            uuid4() if fault == "ledger" else s["ledger"],
            uuid4() if fault == "payment" else original.payment_id,
            sources,
        )
    assert caught.value.orig.diag.constraint_name == "ck_return_revision_capacity"


def test_group_capacity_migration_roundtrip_protects_revision_evidence(revision):
    s, h, _, _ = revision
    migration = runpy.run_path(
        str(
            Path(__file__).resolve().parents[3]
            / "services/api/migrations/versions/20261109_0073_return_revision_capacity.py"
        )
    )
    before = snapshot(s)
    with s["engine"].begin() as c, Operations.context(MigrationContext.configure(c)):
        baseline = definitions(c)
        migration["downgrade"]()
        assert (
            c.scalar(
                text("SELECT to_regprocedure('coinpup_validate_return_capacity(uuid,uuid,uuid[])')")
            )
            is None
        )
        migration["upgrade"]()
        assert definitions(c) == baseline
    with pytest.raises(IntegrityError) as caught, s["engine"].begin() as c:
        c.execute(insert(Revision).values(**h))
        with Operations.context(MigrationContext.configure(c)):
            migration["downgrade"]()
    assert caught.value.orig.diag.constraint_name == "ck_return_revision_downgrade"
    assert snapshot(s) == before
