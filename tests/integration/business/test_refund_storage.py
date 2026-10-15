"""Unvalidated fictional refunds cannot commit; immutable storage retains scoped evidence."""

from datetime import date
from decimal import Decimal
from uuid import uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from coinpup_api.business.refund_models import BusinessRefund, BusinessRefundReturn
from coinpup_api.business.schemas import PartyCreate
from coinpup_api.business.settlement_schemas import BusinessPaymentCreate
from coinpup_api.business.settlements import BusinessSettlementService
from coinpup_api.ledger.schemas import AccountCreate, EntityCreate
from coinpup_api.sync.models import ChangeLog
from sqlalchemy import delete, insert, select, text, update
from sqlalchemy.exc import IntegrityError

from tests.integration.business.test_allocation_return_service import setup_return, submit_return
from tests.integration.business.test_draft_service import drafts as drafts
from tests.integration.conftest import refund_storage_migration
from tests.integration.ledger.test_account_classes import snapshot
from tests.integration.ledger.test_posting_service_database import ledger_setup as ledger_setup

pytestmark = pytest.mark.integration


def prepare(s):
    payment, paid, _, payload = setup_return(s)
    returned = submit_return(s, payload)
    group = dict(
        id=uuid4(),
        ledger_id=s["ledger"],
        payment_id=payment.id,
        source_version=1,
        account_id=payment.account_id,
        asset_id=payment.asset_id,
        amount=Decimal("20"),
        payment_amount=Decimal("20"),
        transaction_date=date(2026, 10, 2),
        reason="Fictional gated refund 中文",
        fees=[],
        created_by=s["owner"],
        idempotency_key="fictional-gated-refund",
        operation_id=paid.operation_id,
        journal_id=paid.journal_id,
    )
    member = dict(
        id=uuid4(),
        ledger_id=s["ledger"],
        refund_id=group["id"],
        payment_id=payment.id,
        return_id=returned.id,
        line_no=1,
    )
    return group, member


def pending(c, group, member):
    c.execute(insert(BusinessRefund).values(**group))
    c.execute(insert(BusinessRefundReturn).values(**member))


def test_refund_notifications_are_atomic_and_unvalidated_postings_cannot_commit(drafts):
    s = drafts
    group, member = prepare(s)
    before = snapshot(s)
    with pytest.raises(IntegrityError) as error, s["engine"].begin() as c:
        pending(c, group, member)
        recorded = c.scalars(
            select(ChangeLog.entity_type).where(
                ChangeLog.entity_id.in_([str(group["id"]), str(member["id"])])
            )
        ).all()
        assert set(recorded) == {"business_refunds", "business_refund_returns"}
        c.exec_driver_sql("SET CONSTRAINTS tr_business_refunds_complete IMMEDIATE")
    assert error.value.orig.diag.constraint_name == "ck_business_refund_shape"
    assert snapshot(s) == before


@pytest.mark.parametrize("table", [BusinessRefund, BusinessRefundReturn])
@pytest.mark.parametrize("action", ["update", "delete"])
def test_refund_and_return_references_cannot_be_rewritten_even_before_commit(drafts, table, action):
    s = drafts
    group, member = prepare(s)
    before = snapshot(s)
    with pytest.raises(IntegrityError) as error, s["engine"].begin() as c:
        pending(c, group, member)
        statement = update(table).values(version=1) if action == "update" else delete(table)
        c.execute(statement)
    assert error.value.orig.diag.constraint_name == "ck_business_refund_immutable"
    assert snapshot(s) == before


@pytest.mark.parametrize(
    "field,value,constraint",
    [
        ("amount", Decimal("0"), "ck_business_refund_amounts"),
        ("payment_amount", Decimal("-1"), "ck_business_refund_amounts"),
        ("source_version", 0, "ck_business_refund_source_version"),
        ("reason", " ", "ck_business_refund_reason"),
        ("fees", {}, "ck_business_refund_fees"),
    ],
)
def test_refund_storage_rejects_invalid_principal_and_source_fields(
    drafts, field, value, constraint
):
    s = drafts
    group, _ = prepare(s)
    before = snapshot(s)
    with pytest.raises(IntegrityError) as error, s["engine"].begin() as c:
        c.execute(insert(BusinessRefund).values(**(group | {field: value})))
    assert error.value.orig.diag.constraint_name == constraint
    assert snapshot(s) == before


def another_payment(s, ledger, account, party):
    return BusinessSettlementService(s["engine"]).pay(
        s["owner"],
        ledger,
        BusinessPaymentCreate(
            id=uuid4(),
            direction="receipt",
            party_id=party,
            account_id=account,
            asset_id="USD",
            amount="20",
            transaction_date="2026-09-15",
            description="Fictional other refund source",
        ),
        "fictional-other-refund-source",
    )


def test_refund_return_reference_must_keep_the_exact_payment_scope(drafts):
    s = drafts
    group, member = prepare(s)
    other = another_payment(s, s["ledger"], s["account"].id, s["party"].id)
    group.update(payment_id=other.id, operation_id=other.operation_id, journal_id=other.journal_id)
    member["payment_id"] = other.id
    before = snapshot(s)
    with pytest.raises(IntegrityError) as error, s["engine"].begin() as c:
        c.execute(insert(BusinessRefund).values(**group))
        c.execute(insert(BusinessRefundReturn).values(**member))
    assert error.value.orig.diag.constraint_name == "fk_business_refund_return_source"
    assert snapshot(s) == before


def test_existing_payment_in_another_owned_ledger_cannot_be_refunded_here(drafts):
    s = drafts
    group, _ = prepare(s)
    entity = s["structure"].create_entity(
        s["owner"],
        EntityCreate(
            kind="personal",
            name="Fictional refund isolation",
            base_asset_id="USD",
            template_key="personal_default",
        ),
    )
    ledger = entity.ledger.id
    account = s["structure"].create_account(
        s["owner"],
        ledger,
        AccountCreate(name="Fictional refund bank", kind="bank", asset_ids=["USD"]),
    )
    party = s["master"].create_party(
        s["owner"],
        ledger,
        PartyCreate(id=uuid4(), name="Fictional other refund party", role="both"),
    )
    other = another_payment(s, ledger, account.id, party.id)
    before = snapshot(s)
    with pytest.raises(IntegrityError) as error, s["engine"].begin() as c:
        c.execute(insert(BusinessRefund).values(**(group | {"payment_id": other.id})))
    assert error.value.orig.diag.constraint_name == "fk_business_refund_payment"
    assert snapshot(s) == before


def test_empty_refund_round_trip_and_historical_notifications_protect_downgrade(drafts):
    s = drafts
    migration = refund_storage_migration()
    with s["engine"].begin() as c, Operations.context(MigrationContext.configure(c)):
        migration["downgrade"]()
        migration["upgrade"]()
    before = snapshot(s)
    with pytest.raises(IntegrityError) as error, s["engine"].begin() as c:
        c.exec_driver_sql(
            "CREATE TEMP TABLE business_refunds(id uuid, ledger_id uuid, version int) "
            "ON COMMIT DROP; "
            "CREATE TRIGGER fictional_refund_history AFTER INSERT ON pg_temp.business_refunds "
            "FOR EACH ROW EXECUTE FUNCTION public.coinpup_record_change()"
        )
        c.execute(
            text("INSERT INTO pg_temp.business_refunds VALUES (:id,:ledger,1)"),
            {"id": uuid4(), "ledger": s["ledger"]},
        )
        with Operations.context(MigrationContext.configure(c)):
            migration["downgrade"]()
    assert error.value.orig.diag.constraint_name == "ck_business_refund_downgrade"
    assert snapshot(s) == before
