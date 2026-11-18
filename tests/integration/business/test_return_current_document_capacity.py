"""Reinstating an application must still fit the current confirmed document on every dimension."""

from datetime import timedelta
from uuid import uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from coinpup_api.business.allocation_return_revisions import BusinessAllocationReturnRevisionService
from coinpup_api.business.allocation_return_schemas import BusinessAllocationReturnCreate
from coinpup_api.business.allocation_returns import BusinessAllocationReturnService
from coinpup_api.business.drafts import PARTY_SNAPSHOT_FIELDS, _snapshot
from coinpup_api.business.schemas import PartyCreate
from coinpup_api.business.settlement_models import BusinessAllocationLine
from coinpup_api.business.settlement_schemas import BusinessAllocationCreate
from coinpup_api.business.settlements import BusinessSettlementService
from coinpup_api.ledger.control_accounts import ControlAccounts
from coinpup_api.ledger.models import FinancialOperation
from coinpup_api.ledger.service import LedgerError
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from tests.integration.business.test_allocation_document_facts import source_payment
from tests.integration.business.test_confirmed_document_revision_headers import cancelled
from tests.integration.business.test_confirmed_document_revision_headers import revision as revision
from tests.integration.business.test_document_accrual_operation_heads import append_accrual
from tests.integration.business.test_draft_service import drafts as drafts
from tests.integration.business.test_return_revision_service import cancel
from tests.integration.conftest import return_document_capacity_migration
from tests.integration.ledger.test_account_classes import snapshot
from tests.integration.ledger.test_posting_service_database import ledger_setup as ledger_setup

pytestmark = pytest.mark.integration


def full_return(s, doc):
    payment = source_payment(s, doc)
    allocation = BusinessAllocationCreate(
        id=uuid4(),
        payment_id=payment.id,
        allocation_date=doc.issue_date,
        lines=[
            dict(
                document_id=doc.id,
                document_line_id=doc.lines[0].id,
                payment_amount="10",
                document_amount="10",
            )
        ],
    )
    BusinessSettlementService(s["engine"]).allocate(
        s["owner"], s["ledger"], allocation, "fictional-ten-dollars"
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
        return_date=doc.issue_date,
        reason="Fictional full release",
        lines=[
            dict(
                source_allocation_line_id=source,
                expected_version=1,
                payment_amount="10",
                document_amount="10",
            )
        ],
    )
    returned = BusinessAllocationReturnService(s["engine"]).create(
        s["owner"], s["ledger"], body, "fictional-full-release"
    )
    return payment.id, source, returned


def change_document(c, h, doc, prior, change):
    price = "9.99" if change == "insufficient" else "10"
    fact = dict(
        quantity="1",
        unit_price=price,
        discount_amount="0",
        tax_rate_percent="0",
        net_amount=price,
        tax_amount="0",
        total_amount=price,
    )
    if change == "asset":
        h = h | dict(asset_id="BTC")
    if change == "project":
        fact.update(project_id=None, project_snapshot=None)
    if change == "date":
        h = h | dict(issue_date=doc.issue_date + timedelta(days=1))
    terminal = change == "cancel"
    append_accrual(
        c,
        cancelled(h) if terminal else h,
        doc,
        prior,
        "cancel" if terminal else "correct",
        2,
        fact_changes=fact,
    )
    c.execute(select(func.coinpup_validate_operation_history(prior.operation_id)))
    c.exec_driver_sql(
        "SET CONSTRAINTS tr_document_accrual_journals,tr_document_accrual_facts,"
        "tr_document_accrual_head,tr_journals_reversal_dimensions IMMEDIATE"
    )


@pytest.mark.parametrize(
    "change", ["at-limit", "insufficient", "asset", "project", "party", "date", "cancel"]
)
def test_full_return_reactivation_obeys_live_price_asset_project_and_date(revision, change):
    s, h, doc, _, origin = revision
    payment, source, returned = full_return(s, doc)
    with Session(s["engine"]) as db, db.begin():
        ControlAccounts(s["engine"]).ensure(
            db, s["owner"], s["ledger"], {"receivable.customer": {"BTC"}}
        )
    if change == "party":
        other = s["master"].create_party(
            s["owner"],
            s["ledger"],
            PartyCreate(id=uuid4(), name="Fictional second customer", role="customer"),
        )
        h = h | dict(party_id=other.id, party_snapshot=_snapshot(other, PARTY_SNAPSHOT_FIELDS))
    before = snapshot(s)
    with s["engine"].connect() as c, c.begin() as tx:
        cap = select(func.coinpup_validate_return_capacity(s["ledger"], payment, [source]))
        revise = BusinessAllocationReturnRevisionService(c)
        with c.begin_nested() as planned:
            change_document(c, h, doc, origin.postings[0], change)
            c.execute(
                cap
            )  # A fully returned allocation leaves zero exposure on every later document version.
            if change == "at-limit":
                assert (
                    revise.cancel(
                        s["owner"], s["ledger"], returned.id, cancel(1), "fictional-reactivate"
                    ).version
                    == 2
                )
                c.execute(cap)
            else:
                with pytest.raises(LedgerError) as refused, c.begin_nested():
                    revise.cancel(
                        s["owner"], s["ledger"], returned.id, cancel(1), "fictional-reactivate"
                    )
                assert (refused.value.code, refused.value.status) == (
                    "allocation_return_document_capacity",
                    409,
                )
            planned.rollback()
        # Real cancellation/new postings exist before the staged incompatible document.
        revise.cancel(s["owner"], s["ledger"], returned.id, cancel(1), "fictional-reactivate")
        change_document(c, h, doc, origin.postings[0], change)
        if change == "at-limit":
            c.execute(cap)
        else:
            with pytest.raises(IntegrityError) as rejected, c.begin_nested():
                c.execute(cap)
            assert (
                rejected.value.orig.diag.constraint_name == "ck_return_revision_document_capacity"
            )
        assert (
            c.scalar(
                select(FinancialOperation.version).where(
                    FinancialOperation.id == returned.postings[0].operation_id
                )
            )
            == 2
        )
        with pytest.raises(IntegrityError) as guarded, c.begin_nested():
            with Operations.context(MigrationContext.configure(c)):
                return_document_capacity_migration()["downgrade"]()
        assert guarded.value.orig.diag.constraint_name == "ck_return_document_capacity_downgrade"
        tx.rollback()
    assert snapshot(s) == before


def test_proposed_deltas_require_the_exact_scoped_sources(revision):
    s, _, doc, _, _ = revision
    _, source, _ = full_return(s, doc)
    call = func.coinpup_return_document_capacity_ok
    with s["engine"].connect() as c:
        assert c.scalar(select(call(s["ledger"], [source], [10])))
        assert not c.scalar(select(call(uuid4(), [source], [1])))
        assert not c.scalar(select(call(s["ledger"], [source, uuid4()], [1, 0])))
        assert not c.scalar(select(call(s["ledger"], [source], [1, 0])))
        assert not c.scalar(select(call(s["ledger"], [source], [None])))
        assert not c.scalar(select(call(s["ledger"], [source], [-1])))
        assert not c.scalar(select(call(s["ledger"], [source], [11])))
