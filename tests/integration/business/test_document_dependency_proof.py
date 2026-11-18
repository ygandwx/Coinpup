"""Recheck live dependencies after document changes, before opening the complete-write gate."""

from uuid import uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from coinpup_api.business.allocation_return_revisions import BusinessAllocationReturnRevisionService
from coinpup_api.business.credit_revision_schemas import CreditCancellationCreate
from coinpup_api.business.credit_revisions import BusinessCreditRevisionService
from coinpup_api.business.drafts import PARTY_SNAPSHOT_FIELDS, _snapshot
from coinpup_api.business.schemas import PartyCreate
from coinpup_api.business.settlement_schemas import BusinessAllocationCreate
from coinpup_api.business.settlements import BusinessSettlementService
from coinpup_api.ledger.control_accounts import ControlAccounts
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from tests.integration.business.test_allocation_document_facts import source_payment
from tests.integration.business.test_confirmed_document_revision_headers import revision as revision
from tests.integration.business.test_credit_current_document_capacity import stage
from tests.integration.business.test_credit_document_sources import sources as sources
from tests.integration.business.test_credit_revision_storage import definitions
from tests.integration.business.test_credit_service import credit
from tests.integration.business.test_document_accrual_operation_heads import append_accrual
from tests.integration.business.test_draft_service import drafts as drafts
from tests.integration.business.test_return_current_document_capacity import (
    change_document,
    full_return,
)
from tests.integration.business.test_return_revision_service import cancel
from tests.integration.conftest import document_dependency_migration
from tests.integration.ledger.test_account_classes import snapshot
from tests.integration.ledger.test_posting_service_database import ledger_setup as ledger_setup

pytestmark = pytest.mark.integration


def prove(c, ledger, document):
    c.execute(select(func.coinpup_validate_document_dependencies(ledger, document)))


@pytest.mark.parametrize("released", [False, True])
@pytest.mark.parametrize(
    "change", ["at-limit", "insufficient", "asset", "project", "party", "date", "cancel"]
)
def test_all_applications_keep_their_current_dimensions_until_explicitly_returned(
    revision, released, change
):
    s, h, doc, _, confirmed = revision
    _, _, returned = full_return(s, doc)
    if not released:
        BusinessAllocationReturnRevisionService(s["engine"]).cancel(
            s["owner"], s["ledger"], returned.id, cancel(1), "fictional-release-reversed"
        )
    with Session(s["engine"]) as db, db.begin():
        ControlAccounts(s["engine"]).ensure(
            db, s["owner"], s["ledger"], {"receivable.customer": {"BTC"}}
        )
    if change == "party":
        party = s["master"].create_party(
            s["owner"],
            s["ledger"],
            PartyCreate(id=uuid4(), name="Fictional dependency party", role="customer"),
        )
        h = h | dict(party_id=party.id, party_snapshot=_snapshot(party, PARTY_SNAPSHOT_FIELDS))
    before = snapshot(s)
    with s["engine"].connect() as c, c.begin() as tx:
        prove(c, s["ledger"], doc.id)
        change_document(c, h, doc, confirmed.postings[0], change)
        if released or change == "at-limit":
            prove(c, s["ledger"], doc.id)
        else:
            with pytest.raises(IntegrityError) as rejected, c.begin_nested():
                prove(c, s["ledger"], doc.id)
            assert rejected.value.orig.diag.constraint_name == (
                "ck_document_dependency_capacity"
                if change in ("insufficient", "cancel")
                else "ck_document_dependency_sources"
            )
        with pytest.raises(IntegrityError) as gated, c.begin_nested():
            c.exec_driver_sql("SET CONSTRAINTS tr_document_revisions_complete IMMEDIATE")
        assert gated.value.orig.diag.constraint_name == "ck_document_revision_complete"
        tx.rollback()
    assert snapshot(s) == before


@pytest.mark.parametrize("released", [False, True])
@pytest.mark.parametrize("change", ["at-limit", "removed", "zero", "cancel", "date"])
def test_credit_only_sources_are_checked_even_without_any_allocation(sources, released, change):
    s, h, doc, confirmed, original, _, payload = sources
    if released:
        BusinessCreditRevisionService(s["engine"]).cancel(
            s["owner"],
            s["ledger"],
            original.id,
            CreditCancellationCreate(expected_version=1, reason="Fictional credit release"),
            "fictional-credit-release",
        )
    before = snapshot(s)
    with s["engine"].connect() as c, c.begin() as tx:
        prove(c, s["ledger"], doc.id)
        stage(c, h, doc, confirmed.postings[0], change)
        if released or change == "at-limit":
            prove(c, s["ledger"], doc.id)
        else:
            with pytest.raises(IntegrityError) as rejected, c.begin_nested():
                prove(c, s["ledger"], doc.id)
            assert rejected.value.orig.diag.constraint_name == "ck_document_dependency_capacity"
        tx.rollback()
    assert credit(s, payload) == original and snapshot(s) == before


def test_net_application_and_credit_share_capacity_and_need_explicit_release(sources):
    s, h, doc, confirmed, original, _, payload = sources
    _, _, returned = full_return(s, doc)
    BusinessAllocationReturnRevisionService(s["engine"]).cancel(
        s["owner"], s["ledger"], returned.id, cancel(1), "fictional-return-reversed"
    )
    before = snapshot(s)
    with s["engine"].connect() as c, c.begin() as tx:
        prove(c, s["ledger"], doc.id)
        change_document(c, h, doc, confirmed.postings[0], "at-limit")
        with pytest.raises(IntegrityError) as rejected, c.begin_nested():
            prove(c, s["ledger"], doc.id)
        assert rejected.value.orig.diag.constraint_name == "ck_document_dependency_capacity"
        BusinessCreditRevisionService(c).cancel(
            s["owner"],
            s["ledger"],
            original.id,
            CreditCancellationCreate(expected_version=1, reason="Fictional explicit release"),
            "fictional-credit-released",
        )
        prove(c, s["ledger"], doc.id)
        tx.rollback()
    assert credit(s, payload) == original and snapshot(s) == before


def test_scope_errors_precede_dependency_reads_and_empty_migration_is_reversible(revision):
    s, _, doc, _, _ = revision
    before = snapshot(s)
    with s["engine"].begin() as c, Operations.context(MigrationContext.configure(c)):
        for ledger, document in (
            (uuid4(), doc.id),
            (s["ledger"], uuid4()),
            (None, doc.id),
            (s["ledger"], None),
        ):
            with pytest.raises(IntegrityError) as rejected, c.begin_nested():
                prove(c, ledger, document)
            assert rejected.value.orig.diag.constraint_name == "ck_document_dependency_scope"
        original = definitions(c)
        migration = document_dependency_migration()
        migration["downgrade"]()
        migration["upgrade"]()
        assert definitions(c) == original
        prove(c, s["ledger"], doc.id)
    assert snapshot(s) == before


def test_existing_document_history_blocks_dependency_proof_downgrade(revision):
    s, h, doc, _, confirmed = revision
    before = snapshot(s)
    with pytest.raises(IntegrityError) as guarded, s["engine"].begin() as c:
        change_document(c, h, doc, confirmed.postings[0], "at-limit")
        with Operations.context(MigrationContext.configure(c)):
            document_dependency_migration()["downgrade"]()
    assert guarded.value.orig.diag.constraint_name == "ck_document_dependency_downgrade"
    assert snapshot(s) == before


def test_repeated_committed_applications_have_no_400_history_limit(revision):
    s, h, doc, _, confirmed = revision
    payment = source_payment(s, doc)
    service = BusinessSettlementService(s["engine"])
    for number in range(401):
        service.allocate(
            s["owner"],
            s["ledger"],
            BusinessAllocationCreate(
                id=uuid4(),
                payment_id=payment.id,
                allocation_date=doc.issue_date,
                lines=[
                    dict(
                        document_id=doc.id,
                        document_line_id=doc.lines[0].id,
                        payment_amount="0.01",
                        document_amount="0.01",
                    )
                ],
            ),
            f"fictional-historical-application-{number}",
        )
    before = snapshot(s)
    with s["engine"].connect() as c, c.begin() as tx:
        prove(c, s["ledger"], doc.id)
        append_accrual(
            c,
            h,
            doc,
            confirmed.postings[0],
            "correct",
            2,
            fact_changes=dict(
                quantity="1",
                unit_price="4.01",
                discount_amount="0",
                tax_rate_percent="0",
                net_amount="4.01",
                tax_amount="0",
                total_amount="4.01",
            ),
        )
        prove(c, s["ledger"], doc.id)
        tx.rollback()
    assert snapshot(s) == before
