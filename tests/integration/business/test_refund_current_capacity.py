"""All three capacity consumers count the same effective payment-side principal."""

from decimal import Decimal
from uuid import uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from coinpup_api.business.refund_revision_models import BusinessRefundRevision as Revision
from coinpup_api.business.settlement_models import BusinessAllocationLine
from coinpup_api.business.settlement_totals import payment_totals
from sqlalchemy import insert, select, text
from sqlalchemy.exc import IntegrityError

from tests.integration.business.test_credit_revision_storage import definitions
from tests.integration.business.test_draft_service import drafts as drafts
from tests.integration.business.test_refund_revision_evidence import evidence as evidence
from tests.integration.business.test_refund_revision_headers import replacement
from tests.integration.business.test_refund_service import refund
from tests.integration.conftest import refund_current_capacity_migration
from tests.integration.ledger.test_account_classes import snapshot
from tests.integration.ledger.test_posting_service_database import ledger_setup as ledger_setup

pytestmark = pytest.mark.integration


@pytest.mark.parametrize(
    "action,principal,over_capacity",
    [
        ("correct", "20.01", True),
        ("correct", "20.00", False),
        ("correct", "0.01", False),
        ("cancel", "0", False),
    ],
)
def test_all_capacity_consumers_follow_the_same_current_principal(
    evidence, action, principal, over_capacity
):
    s, header, command, original, _ = evidence
    before = snapshot(s)
    with s["engine"].connect() as connection:
        payment = original.payment_id
        allocation = connection.scalar(
            select(BusinessAllocationLine.allocation_id).where(
                BusinessAllocationLine.payment_id == payment,
                BusinessAllocationLine.ledger_id == s["ledger"],
            )
        )
        assert allocation is not None
        assert payment_totals(connection, s["ledger"], [payment]) == {
            payment: (Decimal("80"), Decimal("20")),
        }
        if action == "correct":
            header |= replacement(command) | dict(
                asset_id="EUR", amount=Decimal("0.03"), payment_amount=Decimal(principal)
            )
        connection.execute(insert(Revision).values(**header))
        assert payment_totals(connection, s["ledger"], [payment, payment, uuid4()]) == {
            payment: (Decimal("80"), Decimal(principal)),
        }
        assert payment_totals(connection, uuid4(), [payment]) == {}
        for kind, identifier in (("refund", original.id), ("allocation", allocation)):
            validate = text(f"SELECT public.coinpup_validate_business_{kind}(:id)")
            if over_capacity:
                with pytest.raises(IntegrityError) as rejected, connection.begin_nested():
                    connection.execute(validate, {"id": identifier})
                assert rejected.value.orig.diag.constraint_name == f"ck_business_{kind}_shape"
            else:
                connection.execute(validate, {"id": identifier})
        with pytest.raises(IntegrityError) as gated:
            connection.exec_driver_sql(
                "SET CONSTRAINTS tr_business_refund_revisions_complete IMMEDIATE"
            )
        assert gated.value.orig.diag.constraint_name == "ck_refund_revision_shape"
        connection.rollback()
    assert refund(s, command) == original
    assert snapshot(s) == before


def test_empty_capacity_round_trip_restores_exact_functions_and_keeps_original_receipt(evidence):
    s, _, command, original, _ = evidence
    before = snapshot(s)
    migration = refund_current_capacity_migration()
    with s["engine"].begin() as c, Operations.context(MigrationContext.configure(c)):
        current = definitions(c)
        migration["downgrade"]()
        for kind in ("allocation", "refund"):
            body = c.execute(
                text("SELECT prosrc FROM pg_proc WHERE oid=CAST(:signature AS regprocedure)"),
                {"signature": f"public.coinpup_validate_business_{kind}(uuid)"},
            ).scalar_one()
            # PostgreSQL preserves the body text between the original dollar quotes.
            assert migration[f"_OLD_{kind.upper()}"].split("$$")[1] == body
        migration["upgrade"]()
        assert definitions(c) == current
    assert refund(s, command) == original
    assert snapshot(s) == before


def test_capacity_downgrade_rejects_pending_history_without_changes(evidence):
    s, header, command, original, _ = evidence
    before = snapshot(s)
    with pytest.raises(IntegrityError) as rejected, s["engine"].begin() as c:
        c.execute(insert(Revision).values(**header))
        with Operations.context(MigrationContext.configure(c)):
            refund_current_capacity_migration()["downgrade"]()
    assert rejected.value.orig.diag.constraint_name == "ck_refund_revision_downgrade"
    assert refund(s, command) == original
    assert snapshot(s) == before
