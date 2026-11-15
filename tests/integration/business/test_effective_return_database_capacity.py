"""Three DB validators and application readers must agree on whole active return groups."""

from decimal import Decimal
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
from coinpup_api.business.settlement_models import BusinessAllocationLine
from coinpup_api.business.settlement_totals import document_totals, payment_totals
from sqlalchemy import insert, select, text
from sqlalchemy.exc import IntegrityError

from tests.integration.business.test_allocation_return_revision_headers import revision as revision
from tests.integration.business.test_allocation_return_revision_members import member
from tests.integration.business.test_credit_revision_storage import definitions
from tests.integration.business.test_credit_service import credit
from tests.integration.business.test_draft_service import drafts as drafts
from tests.integration.business.test_refund_service import refund
from tests.integration.conftest import effective_return_capacity_migration
from tests.integration.ledger.test_account_classes import snapshot
from tests.integration.ledger.test_posting_service_database import ledger_setup as ledger_setup

pytestmark = pytest.mark.integration


def totals(c, ledger, payment, line):
    return c.execute(
        text(
            "SELECT coinpup_returned_payment_amount(:ledger,:payment), "
            "coinpup_returned_document_amount(:ledger,:line)"
        ),
        {"ledger": ledger, "payment": payment, "line": line},
    ).one()


@pytest.mark.parametrize("action", ["correct", "cancel"])
def test_effective_database_terms_match_both_asset_sides_and_scoped_python_reads(revision, action):
    s, h, _, orig = revision
    line = orig.postings[0]
    before = snapshot(s)
    with s["engine"].connect() as c:
        assert totals(c, s["ledger"], orig.payment_id, line.document_line_id) == (20, 20)
        m = member(h, orig)
        if action == "cancel":
            h.update(action="cancel", return_date=None, return_reason=None)
            m.update(
                action="cancel",
                line_no=None,
                journal_id=None,
                payment_amount=None,
                document_amount=None,
                absorbs_remainder=None,
            )
        c.execute(insert(Revision).values(**h))
        c.execute(insert(Member).values(**m))
        expected = Decimal("12.34") if action == "correct" else Decimal(0)
        assert totals(c, s["ledger"], orig.payment_id, line.document_line_id) == (
            expected,
            expected,
        )
        assert totals(c, uuid4(), orig.payment_id, line.document_line_id) == (0, 0)
        assert totals(c, s["ledger"], uuid4(), uuid4()) == (0, 0)
        assert payment_totals(c, s["ledger"], [orig.payment_id])[orig.payment_id] == (
            100 - expected,
            0,
        )
        assert document_totals(c, s["ledger"], [line.document_line_id])[line.document_line_id] == (
            100 - expected,
            0,
            0,
        )
        c.rollback()
    assert snapshot(s) == before


def test_four_capacity_uses_and_their_predecessors_are_exact_on_empty_roundtrip(revision):
    s, _, _, orig = revision
    mig = effective_return_capacity_migration()
    before = snapshot(s)
    names = ("allocation", "credit", "refund")

    def sql(c, name):
        return c.scalar(
            text("SELECT pg_get_functiondef(to_regprocedure(:fn))"),
            {"fn": f"public.coinpup_validate_business_{name}(uuid)"},
        )

    with s["engine"].begin() as c, Operations.context(MigrationContext.configure(c)):
        current = definitions(c)
        for name in names:
            actual = sql(c, name)
            assert actual == mig["_" + name.upper()]
            assert actual.count("coinpup_returned_") == (2 if name == "allocation" else 1)
        mig["downgrade"]()
        for name in names:
            assert sql(c, name) == mig["_OLD_" + name.upper()]
        mig["upgrade"]()
        assert definitions(c) == current
    assert snapshot(s) == before


def test_existing_return_rows_guard_downgrade_before_any_validator_is_replaced(revision):
    s, h, _, _ = revision
    before = snapshot(s)
    with pytest.raises(IntegrityError) as failure, s["engine"].begin() as c:
        c.execute(insert(Revision).values(**h))
        with Operations.context(MigrationContext.configure(c)):
            effective_return_capacity_migration()["downgrade"]()
    assert (
        failure.value.orig.diag.constraint_name == "ck_return_revision_downgrade"
        and snapshot(s) == before
    )


@pytest.mark.parametrize("kind", ["allocation", "refund", "credit"])
def test_actual_deferred_validator_rejects_a_shortened_return_even_though_v1_fits(revision, kind):
    s, header, _, original = revision
    line = original.postings[0]
    with s["engine"].connect() as c:
        version = c.scalar(
            select(BusinessDocument.version).where(BusinessDocument.id == line.document_id)
        )
        allocation_id = c.scalar(
            select(BusinessAllocationLine.allocation_id).where(
                BusinessAllocationLine.id == line.source_allocation_line_id
            )
        )
    if kind == "credit":
        body = BusinessCreditCreate(
            id=uuid4(),
            document_id=line.document_id,
            expected_version=version,
            credit_date="2026-10-06",
            reason="Fictional credit consuming released capacity",
            lines=[
                dict(
                    source_line_id=line.document_line_id,
                    net_amount="20",
                    tax_amount="0",
                    recognition_date="2026-10-06",
                )
            ],
        )
        subject = credit(s, body).id
    else:
        body = BusinessRefundCreate(
            id=uuid4(),
            payment_id=original.payment_id,
            expected_version=1,
            account_id=s["account"].id,
            asset_id="USD",
            amount="20",
            payment_amount="20",
            transaction_date="2026-10-06",
            reason="Fictional refund consuming released principal",
            allocation_return_ids=[],
        )
        cash = refund(s, body)
        subject = cash.id if kind == "refund" else allocation_id
    check = text(f"SELECT coinpup_validate_business_{kind}(:subject)")
    before = snapshot(s)
    with s["engine"].connect() as c:
        c.execute(check, {"subject": subject})
        c.execute(insert(Revision).values(**header))
        c.execute(
            insert(Member).values(
                **(
                    member(header, original)
                    | {"payment_amount": Decimal("5"), "document_amount": Decimal("5")}
                )
            )
        )
        with pytest.raises(IntegrityError) as caught:
            c.execute(check, {"subject": subject})
        assert caught.value.orig.diag.constraint_name == f"ck_business_{kind}_shape"
        c.rollback()
    assert snapshot(s) == before
