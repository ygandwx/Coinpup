"""An exact ledger/journal lookup retains each refund's immutable posting facts."""

from decimal import Decimal
from uuid import uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from coinpup_api.business.refund_revision_models import BusinessRefundRevision as Revision
from coinpup_api.business.refund_revision_return_models import (
    BusinessRefundRevisionReturn as Evidence,
)
from coinpup_api.business.refund_schemas import BusinessRefundCreate
from sqlalchemy import insert, text
from sqlalchemy.exc import IntegrityError

from tests.integration.business.test_allocation_return_service import setup_return, submit_return
from tests.integration.business.test_draft_service import drafts as drafts
from tests.integration.business.test_refund_revision_evidence import evidence as evidence
from tests.integration.business.test_refund_revision_headers import refund_header, replacement
from tests.integration.business.test_refund_service import refund
from tests.integration.conftest import refund_posting_fact_migration
from tests.integration.ledger.test_account_classes import snapshot
from tests.integration.ledger.test_posting_service_database import ledger_setup as ledger_setup

pytestmark = pytest.mark.integration


def fact(connection, ledger, journal):
    return (
        connection.execute(
            text("SELECT * FROM public.coinpup_refund_posting_fact(:ledger,:journal)"),
            {"ledger": ledger, "journal": journal},
        )
        .mappings()
        .one_or_none()
    )


def test_posting_and_replacement_facts_survive_later_cancellation_in_closed_transaction(evidence):
    s, header, command, original, link = evidence
    before = snapshot(s)
    with s["engine"].connect() as c:
        origin = fact(c, s["ledger"], original.journal_id)
        assert dict(origin) == dict(
            ledger_id=s["ledger"],
            refund_id=original.id,
            payment_id=original.payment_id,
            operation_id=original.operation_id,
            journal_id=original.journal_id,
            operation_version=1,
            revision_id=None,
            revision_reason=None,
            created_by=s["owner"],
            account_id=command.account_id,
            asset_id="USD",
            amount=Decimal("20"),
            payment_asset_id="USD",
            payment_amount=Decimal("20"),
            source_version=1,
            transaction_date=command.transaction_date,
            recognition_date=command.transaction_date,
            reason=command.reason,
            fees=[],
            allocation_return_ids=[link["return_id"]],
        )
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
                        category_id=str(s["expenses"][0].id),
                        asset_id="USD",
                        amount=value,
                    )
                    for value in ("0.02", "0.01")
                ],
            )
        )
        c.execute(insert(Revision).values(**corrected))
        c.execute(insert(Evidence).values(**link))
        fixed = fact(c, s["ledger"], corrected["replacement_journal_id"])
        assert dict(fixed) == dict(origin) | dict(
            journal_id=corrected["replacement_journal_id"],
            operation_version=2,
            revision_id=header["id"],
            revision_reason=header["revision_reason"],
            asset_id="EUR",
            amount=Decimal("8.01"),
            payment_amount=Decimal("9.99"),
            transaction_date=corrected["transaction_date"],
            recognition_date=corrected["transaction_date"],
            reason=corrected["reason"],
            fees=corrected["fees"],
        )
        cancelled = header | dict(
            id=uuid4(),
            previous_revision_id=header["id"],
            operation_version=3,
            previous_journal_id=corrected["replacement_journal_id"],
            reversal_journal_id=uuid4(),
            idempotency_key="fictional-fact-cancel",
        )
        c.execute(insert(Revision).values(**cancelled))
        assert fact(c, s["ledger"], original.journal_id) == origin
        assert fact(c, s["ledger"], corrected["replacement_journal_id"]) == fixed
        for journal in (
            header["reversal_journal_id"],
            cancelled["reversal_journal_id"],
            uuid4(),
            None,
        ):
            assert fact(c, s["ledger"], journal) is None
        assert fact(c, uuid4(), original.journal_id) is None
        assert fact(c, None, corrected["replacement_journal_id"]) is None
        with pytest.raises(IntegrityError) as closed:
            c.exec_driver_sql("SET CONSTRAINTS tr_business_refund_revisions_complete IMMEDIATE")
        assert closed.value.orig.diag.constraint_name == "ck_refund_revision_shape"
        c.rollback()
    assert refund(s, command) == original
    assert snapshot(s) == before


def test_receipt_and_posting_retain_explicit_evidence_order(drafts):
    s = drafts
    payment, _, _, release = setup_return(s, count=2)
    returns = [
        submit_return(
            s,
            release.model_copy(update={"id": uuid4(), "lines": [line]}),
            key=f"fictional-source-{n}",
        ).id
        for n, line in enumerate(release.lines)
    ]
    command = BusinessRefundCreate(
        id=uuid4(),
        payment_id=payment.id,
        expected_version=1,
        account_id=payment.account_id,
        asset_id="USD",
        amount="0.01",
        payment_amount="0.01",
        transaction_date="2026-10-02",
        reason="Fictional explicit evidence",
        allocation_return_ids=returns[::-1],
    )
    saved = refund(s, command)
    before = snapshot(s)
    with s["engine"].connect() as c:
        c.exec_driver_sql("SET TRANSACTION READ ONLY")
        row = fact(c, s["ledger"], saved.journal_id)
        assert row["allocation_return_ids"] == returns[::-1]
        assert row["payment_amount"] == Decimal("0.01")
    assert refund(s, command) == saved and saved.allocation_return_ids == tuple(returns[::-1])
    assert snapshot(s) == before


def test_ambiguous_original_and_other_replacement_are_rejected(drafts):
    s, header, command, first = refund_header(drafts, allocated=False)
    second = refund(
        s,
        command.model_copy(update={"id": uuid4(), "payment_amount": "5", "amount": "5"}),
        key="fictional-other-refund-fact",
    )
    before = snapshot(s)
    with pytest.raises(IntegrityError) as ambiguous, s["engine"].begin() as c:
        c.execute(
            insert(Revision).values(
                **(header | replacement(command) | dict(replacement_journal_id=second.journal_id))
            )
        )
        fact(c, s["ledger"], second.journal_id)
    assert ambiguous.value.orig.diag.constraint_name == "ck_refund_posting_fact"
    assert refund(s, command) == first
    assert snapshot(s) == before


def test_fact_function_empty_downgrade_and_upgrade_leave_original_data_intact(evidence):
    s, _, command, original, _ = evidence
    migration = refund_posting_fact_migration()
    before = snapshot(s)
    with s["engine"].begin() as c, Operations.context(MigrationContext.configure(c)):
        original_fact = fact(c, s["ledger"], original.journal_id)
        lookup = "SELECT pg_get_functiondef('coinpup_refund_posting_fact(uuid,uuid)'::regprocedure)"
        definition = c.exec_driver_sql(lookup).scalar_one()
        migration["downgrade"]()
        signature = "SELECT to_regprocedure('public.coinpup_refund_posting_fact(uuid,uuid)')"
        assert c.exec_driver_sql(signature).scalar_one() is None
        migration["upgrade"]()
        assert c.exec_driver_sql(lookup).scalar_one() == definition
        assert fact(c, s["ledger"], original.journal_id) == original_fact
    assert refund(s, command) == original
    assert snapshot(s) == before


def test_downgrade_requires_no_revision_history(evidence):
    s, header, command, original, _ = evidence
    before = snapshot(s)
    with pytest.raises(IntegrityError) as rejected, s["engine"].begin() as c:
        c.execute(insert(Revision).values(**header))
        with Operations.context(MigrationContext.configure(c)):
            refund_posting_fact_migration()["downgrade"]()
    assert rejected.value.orig.diag.constraint_name == "ck_refund_revision_downgrade"
    assert refund(s, command) == original
    assert snapshot(s) == before
