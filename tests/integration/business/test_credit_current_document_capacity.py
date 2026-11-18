"""Active credit amounts consume current prices; cancelled history consumes no capacity."""

from datetime import timedelta
from uuid import uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from coinpup_api.business.allocation_return_revisions import BusinessAllocationReturnRevisionService
from coinpup_api.business.credit_revision_schemas import CreditCancellationCreate
from coinpup_api.business.credit_revisions import BusinessCreditRevisionService
from coinpup_api.business.document_revision_models import BusinessDocumentRevision as Header
from coinpup_api.business.document_revision_models import BusinessDocumentRevisionLine as Fact
from coinpup_api.business.drafts import CATEGORY_SNAPSHOT_FIELDS, PARTY_SNAPSHOT_FIELDS, _snapshot
from coinpup_api.business.schemas import PartyCreate
from coinpup_api.ledger.control_accounts import ControlAccounts
from coinpup_api.ledger.schemas import CategoryCreate
from sqlalchemy import func, insert, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from tests.integration.business.test_confirmed_document_revision_headers import cancelled
from tests.integration.business.test_confirmed_document_revision_headers import revision as revision
from tests.integration.business.test_credit_current_capacity import validate
from tests.integration.business.test_credit_document_sources import sources as sources
from tests.integration.business.test_credit_revision_storage import definitions
from tests.integration.business.test_credit_service import credit
from tests.integration.business.test_document_accrual_operation_heads import append_accrual
from tests.integration.business.test_document_revision_line_facts import line
from tests.integration.business.test_draft_service import drafts as drafts
from tests.integration.business.test_return_current_document_capacity import full_return
from tests.integration.business.test_return_revision_service import cancel
from tests.integration.conftest import credit_document_capacity_migration
from tests.integration.ledger.test_account_classes import snapshot
from tests.integration.ledger.test_posting_service_database import ledger_setup as ledger_setup

pytestmark = pytest.mark.integration


def capacity(c, ledger, document, ids):
    return c.scalar(select(func.coinpup_credit_document_capacity_ok(ledger, document, ids)))


def stage(c, h, doc, posting, change, category=None):
    fact = dict(
        quantity="1",
        unit_price="0.01",
        discount_amount="0",
        tax_rate_percent="0",
        net_amount="0.01",
        tax_amount="0",
        total_amount="0.01",
    )
    if change == "asset":
        h = h | dict(asset_id="BTC")
    elif change == "project":
        fact.update(project_id=None, project_snapshot=None)
    elif change == "date":
        h = h | dict(issue_date=doc.issue_date + timedelta(days=1))
    elif change == "recognition":
        fact.update(recognition_date=doc.lines[0].recognition_date + timedelta(days=1))
    elif change == "category":
        fact.update(
            category_id=category.id, category_snapshot=_snapshot(category, CATEGORY_SNAPSHOT_FIELDS)
        )
    if change in ("removed", "zero"):
        c.execute(insert(Header), h)
        if change == "zero":
            c.execute(
                insert(Fact),
                line(h, doc) | fact | dict(unit_price="0", net_amount="0", total_amount="0"),
            )
    else:
        terminal = change == "cancel"
        append_accrual(
            c,
            cancelled(h) if terminal else h,
            doc,
            posting,
            "cancel" if terminal else "correct",
            2,
            fact_changes=fact,
        )


@pytest.mark.parametrize("released", [False, True])
@pytest.mark.parametrize(
    "change",
    [
        "at-limit",
        "asset",
        "party",
        "project",
        "category",
        "date",
        "recognition",
        "removed",
        "zero",
        "cancel",
    ],
)
def test_current_scope_dimensions_and_zero_capacity_do_not_reinterpret_cancelled_history(
    sources, change, released
):
    s, h, doc, confirmed, original, _, payload = sources
    category = None
    if change == "category":
        category = s["structure"].create_category(
            s["owner"], s["ledger"], CategoryCreate(name="Fictional revised income", kind="income")
        )
    if change == "asset":
        with Session(s["engine"]) as db, db.begin():
            ControlAccounts(s["engine"]).ensure(
                db, s["owner"], s["ledger"], {"receivable.customer": {"BTC"}}
            )
    if change == "party":
        party = s["master"].create_party(
            s["owner"],
            s["ledger"],
            PartyCreate(id=uuid4(), name="Fictional revised credit party", role="customer"),
        )
        h = h | dict(party_id=party.id, party_snapshot=_snapshot(party, PARTY_SNAPSHOT_FIELDS))
    if released:
        BusinessCreditRevisionService(s["engine"]).cancel(
            s["owner"],
            s["ledger"],
            original.id,
            CreditCancellationCreate(expected_version=1, reason="Fictional explicit release"),
            "fictional-release-before-document",
        )
    before = snapshot(s)
    with s["engine"].connect() as c, c.begin() as tx:
        assert capacity(c, s["ledger"], doc.id, [doc.lines[0].id])
        stage(c, h, doc, confirmed.postings[0], change, category)
        expected = released or change in ("at-limit", "recognition")
        assert capacity(c, s["ledger"], doc.id, [doc.lines[0].id]) is expected
        validate(c, "credit", original.id, exceeds=not expected)
        # Still no complete document command may commit through this preparation layer.
        with pytest.raises(IntegrityError) as refused, c.begin_nested():
            c.exec_driver_sql("SET CONSTRAINTS tr_document_revisions_complete IMMEDIATE")
        assert refused.value.orig.diag.constraint_name == "ck_document_revision_complete"
        tx.rollback()
    assert credit(s, payload) == original and snapshot(s) == before


@pytest.mark.parametrize("part", ["net", "tax", "exact"])
def test_net_and_tax_cannot_borrow_from_each_other_even_when_gross_fits(sources, part):
    s, h, doc, confirmed, _, _, payload = sources
    another = payload.model_copy(
        update={
            "id": uuid4(),
            "lines": [payload.lines[0].model_copy(update={"tax_amount": "0.01"})],
        }
    )
    credit(s, another, "fictional-independent-components")
    prices = {
        "net": ("0.01", "200", "0.02"),
        "tax": ("0.03", "0", "0"),
        "exact": ("0.02", "50", "0.01"),
    }
    net, rate, tax = prices[part]
    before = snapshot(s)
    with s["engine"].connect() as c, c.begin() as tx:
        append_accrual(
            c,
            h,
            doc,
            confirmed.postings[0],
            "correct",
            2,
            fact_changes=dict(
                quantity="1",
                unit_price=net,
                discount_amount="0",
                tax_rate_percent=rate,
                net_amount=net,
                tax_amount=tax,
                total_amount="0.03",
            ),
        )
        assert capacity(c, s["ledger"], doc.id, [doc.lines[0].id]) is (part == "exact")
        tx.rollback()
    assert snapshot(s) == before


def test_scope_and_empty_migration_round_trip_preserve_exact_catalog(sources):
    s, _, doc, _, original, _, payload = sources
    before = snapshot(s)
    with s["engine"].begin() as c, Operations.context(MigrationContext.configure(c)):
        source = doc.lines[0].id
        for ledger, document, ids in (
            (uuid4(), doc.id, [source]),
            (s["ledger"], uuid4(), [source]),
            (s["ledger"], doc.id, []),
            (s["ledger"], doc.id, [source, uuid4()]),
            (s["ledger"], doc.id, [source, None]),
        ):
            assert not capacity(c, ledger, document, ids)
        baseline = definitions(c)
        migration = credit_document_capacity_migration()
        migration["downgrade"]()
        validate(c, "credit", original.id)
        migration["upgrade"]()
        assert definitions(c) == baseline
        validate(c, "credit", original.id)
    assert credit(s, payload) == original and snapshot(s) == before


def test_document_history_blocks_capacity_downgrade(sources):
    s, h, doc, confirmed, _, _, _ = sources
    before = snapshot(s)
    with pytest.raises(IntegrityError) as refused, s["engine"].begin() as c:
        append_accrual(c, h, doc, confirmed.postings[0], "correct", 2)
        with Operations.context(MigrationContext.configure(c)):
            credit_document_capacity_migration()["downgrade"]()
    assert refused.value.orig.diag.constraint_name == "ck_credit_document_capacity_downgrade"
    assert snapshot(s) == before


@pytest.mark.parametrize(
    "reinstated,price,allowed", [(False, "0.01", True), (True, "10.01", True), (True, "10", False)]
)
def test_credit_and_reinstated_allocation_share_current_gross_without_recounting_old_returns(
    sources, reinstated, price, allowed
):
    s, h, doc, confirmed, original, _, payload = sources
    _, _, returned = full_return(s, doc)
    if reinstated:
        BusinessAllocationReturnRevisionService(s["engine"]).cancel(
            s["owner"], s["ledger"], returned.id, cancel(1), "fictional-reinstate-application"
        )
    before = snapshot(s)
    with s["engine"].connect() as c, c.begin() as tx:
        append_accrual(
            c,
            h,
            doc,
            confirmed.postings[0],
            "correct",
            2,
            fact_changes=dict(
                quantity="1",
                unit_price=price,
                discount_amount="0",
                tax_rate_percent="0",
                net_amount=price,
                tax_amount="0",
                total_amount=price,
            ),
        )
        assert capacity(c, s["ledger"], doc.id, [doc.lines[0].id]) is allowed
        validate(c, "credit", original.id, exceeds=not allowed)
        tx.rollback()
    assert credit(s, payload) == original and snapshot(s) == before
