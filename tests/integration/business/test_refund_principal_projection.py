"""Current refund principal is in the fixed payment asset and is counted once per refund."""

from decimal import Decimal
from uuid import uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from coinpup_api.business.refund_revision_models import BusinessRefundRevision as Revision
from coinpup_api.business.refund_revision_return_models import (
    BusinessRefundRevisionReturn as Evidence,
)
from coinpup_api.ledger.schemas import EntityCreate
from sqlalchemy import insert, text
from sqlalchemy.exc import IntegrityError

from tests.integration.business.test_draft_service import drafts as drafts
from tests.integration.business.test_refund_revision_evidence import evidence as evidence
from tests.integration.business.test_refund_revision_headers import refund_header, replacement
from tests.integration.business.test_refund_service import refund
from tests.integration.conftest import refund_principal_migration
from tests.integration.ledger.test_account_classes import snapshot
from tests.integration.ledger.test_posting_service_database import ledger_setup as ledger_setup

pytestmark = pytest.mark.integration


def principals(connection, ledger, payments):
    rows = (
        connection.execute(
            text(
                "SELECT * FROM public.coinpup_current_refund_principals("
                ":ledger, CAST(:ids AS uuid[]))"
            ),
            {"ledger": ledger, "ids": payments},
        )
        .mappings()
        .all()
    )
    assert len({row["refund_id"] for row in rows}) == len(rows)
    assert all(row["ledger_id"] == ledger and row["payment_id"] in payments for row in rows)
    return {row["refund_id"]: (row["operation_version"], row["payment_amount"]) for row in rows}


def test_current_versions_use_payment_side_principal_and_never_resurrect_cancelled_refunds(
    evidence,
):
    s, header, command, original, evidence_row = evidence
    # The fixture refunds all 20 released units; there is no second cash refund in this group.
    before = snapshot(s)
    with s["engine"].connect() as c:
        scope = [original.payment_id, original.payment_id, uuid4()]
        assert principals(c, s["ledger"], scope) == {original.id: (1, Decimal("20"))}
        corrected = (
            header
            | replacement(command)
            | dict(
                asset_id="EUR",
                amount=Decimal("8.01"),
                payment_amount=Decimal("9.99"),
                fees=[
                    dict(
                        account_id=str(command.account_id),
                        asset_id="USD",
                        amount=amount,
                        category_id=str(s["expenses"][0].id),
                    )
                    for amount in ("0.25", "0.25")
                ],
            )
        )
        c.execute(insert(Revision).values(**corrected))
        c.execute(insert(Evidence).values(**evidence_row))
        assert principals(c, s["ledger"], scope) == {original.id: (2, Decimal("9.99"))}

        changed = corrected | dict(
            id=uuid4(),
            operation_version=3,
            previous_revision_id=corrected["id"],
            previous_journal_id=corrected["replacement_journal_id"],
            reversal_journal_id=uuid4(),
            replacement_journal_id=uuid4(),
            idempotency_key="fictional-refund-v3",
            amount=Decimal("0.01"),
            payment_amount=Decimal("0.07"),
            fees=[],
        )
        c.execute(insert(Revision).values(**changed))
        assert principals(c, s["ledger"], scope) == {original.id: (3, Decimal("0.07"))}
        c.execute(
            insert(Revision).values(
                **(
                    header
                    | dict(
                        id=uuid4(),
                        operation_version=4,
                        previous_revision_id=changed["id"],
                        previous_journal_id=changed["replacement_journal_id"],
                        reversal_journal_id=uuid4(),
                        idempotency_key="fictional-refund-v4",
                    )
                )
            )
        )
        assert principals(c, s["ledger"], scope) == {}
        # The intentionally uncommitted headers exercise the projection behind the closed gate.
        with pytest.raises(IntegrityError) as failure:
            c.exec_driver_sql("SET CONSTRAINTS tr_business_refund_revisions_complete IMMEDIATE")
        assert failure.value.orig.diag.constraint_name == "ck_refund_revision_shape"
        c.rollback()
    assert refund(s, command) == original
    assert snapshot(s) == before


def test_same_payment_can_have_multiple_refunds_without_duplicate_amounts(drafts):
    s, _, command, first = refund_header(drafts, allocated=False)
    second_command = command.model_copy(
        update={"id": uuid4(), "amount": "5.01", "payment_amount": "5.01"}
    )
    second = refund(s, second_command, key="fictional-second-small-refund")
    before = snapshot(s)
    with s["engine"].connect() as c:
        c.exec_driver_sql("SET TRANSACTION READ ONLY")
        assert principals(c, s["ledger"], [first.payment_id, first.payment_id]) == {
            first.id: (1, Decimal("20")),
            second.id: (1, Decimal("5.01")),
        }
    assert refund(s, second_command, key="fictional-second-small-refund") == second
    assert snapshot(s) == before


def test_foreign_empty_unknown_and_null_scopes_do_not_return_rows(evidence):
    s, _, _, original, _ = evidence
    other = s["structure"].create_entity(
        s["owner"],
        EntityCreate(
            kind="personal",
            name="Fictional isolated principal",
            base_asset_id="USD",
            template_key="personal_default",
        ),
    )
    with s["engine"].connect() as c:
        assert principals(c, other.ledger.id, [original.payment_id]) == {}
        for identifiers in (None, [], [uuid4()]):
            assert principals(c, s["ledger"], identifiers) == {}


def test_empty_projection_round_trip_preserves_original_refund_and_source_functions(evidence):
    s, _, command, original, _ = evidence
    before = snapshot(s)
    migration = refund_principal_migration()
    with s["engine"].begin() as c, Operations.context(MigrationContext.configure(c)):
        sql = (
            "SELECT pg_get_functiondef("
            "'coinpup_current_refund_principals(uuid,uuid[])'::regprocedure)"
        )
        original_sql = c.exec_driver_sql(sql).scalar_one()
        migration["downgrade"]()
        assert (
            c.exec_driver_sql(
                "SELECT to_regprocedure('coinpup_current_refund_principals(uuid,uuid[])')"
            ).scalar_one()
            is None
        )
        migration["upgrade"]()
        assert c.exec_driver_sql(sql).scalar_one() == original_sql
        assert principals(c, s["ledger"], [original.payment_id]) == {
            original.id: (1, Decimal("20"))
        }
    assert refund(s, command) == original
    assert snapshot(s) == before


def test_pending_refund_history_blocks_principal_downgrade(evidence):
    s, header, _, _, _ = evidence
    with pytest.raises(IntegrityError) as caught, s["engine"].begin() as c:
        c.execute(insert(Revision).values(**header))
        with Operations.context(MigrationContext.configure(c)):
            refund_principal_migration(include_dependents=False)["downgrade"]()
    assert caught.value.orig.diag.constraint_name == "ck_refund_revision_downgrade"
