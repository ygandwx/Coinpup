"""Fictional immutable storage probes keep unvalidated returns from committing."""

from datetime import date
from decimal import Decimal
from uuid import uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from coinpup_api.business.allocation_return_models import (
    BusinessAllocationReturn,
    BusinessAllocationReturnLine,
)
from coinpup_api.business.settlement_models import BusinessAllocationLine
from coinpup_api.ledger.schemas import AccountCreate, EntityCreate
from coinpup_api.sync.models import ChangeLog
from sqlalchemy import delete, insert, select, text, update
from sqlalchemy.exc import IntegrityError

from tests.integration.business.allocation_fixture import source, write
from tests.integration.business.payment_fixture import source as payment_source
from tests.integration.business.payment_fixture import write as payment_write
from tests.integration.business.test_draft_service import drafts as drafts
from tests.integration.conftest import allocation_return_storage_migration
from tests.integration.ledger.test_account_classes import snapshot
from tests.integration.ledger.test_posting_service_database import ledger_setup as ledger_setup

pytestmark = pytest.mark.integration
TABLES = (BusinessAllocationReturn, BusinessAllocationReturnLine)


def rows(s):
    facts = source(s)
    with s["engine"].begin() as c:
        result = write(c, s, *facts)
        original = (
            c.execute(
                select(BusinessAllocationLine).where(
                    BusinessAllocationLine.allocation_id == result["id"]
                )
            )
            .mappings()
            .one()
        )
    group = dict(
        id=uuid4(),
        ledger_id=s["ledger"],
        payment_id=facts[0].id,
        return_date=date(2026, 10, 1),
        reason="Fictional return storage 中文",
        created_by=s["owner"],
        idempotency_key="fictional-allocation-" + result["id"],
    )
    member = dict(
        id=uuid4(),
        ledger_id=s["ledger"],
        return_id=group["id"],
        payment_id=facts[0].id,
        source_allocation_line_id=original["id"],
        source_version=1,
        source_return_no=1,
        line_no=1,
        payment_amount=Decimal("20"),
        document_amount=Decimal("20"),
        operation_id=original["operation_id"],
        journal_id=original["journal_id"],
    )
    return group, member


def pending(c, values):
    for table, row in zip(TABLES, values, strict=True):
        c.execute(insert(table).values(**row))


def test_return_notifications_are_atomic_and_original_mapping_is_not_overwritten(drafts):
    s = drafts
    values = rows(s)
    before = snapshot(s)
    with s["engine"].connect() as c:
        transaction = c.begin()
        try:
            pending(c, values)
            assert set(
                c.execute(
                    select(ChangeLog.entity_type, ChangeLog.entity_id).where(
                        ChangeLog.entity_type.in_([table.__tablename__ for table in TABLES])
                    )
                ).all()
            ) == {
                (table.__tablename__, str(row["id"]))
                for table, row in zip(TABLES, values, strict=True)
            }
            assert c.scalar(select(BusinessAllocationReturnLine.payment_amount)) == Decimal("20")
        finally:
            transaction.rollback()
    assert snapshot(s) == before


@pytest.mark.parametrize("include_line", [False, True])
def test_return_cannot_commit_with_empty_or_legacy_financial_members(drafts, include_line):
    s = drafts
    values = rows(s)
    before = snapshot(s)
    with pytest.raises(IntegrityError) as error, s["engine"].begin() as c:
        c.execute(insert(TABLES[0]).values(**values[0]))
        if include_line:
            c.execute(insert(TABLES[1]).values(**values[1]))
    assert error.value.orig.diag.constraint_name == "ck_business_allocation_return_shape"
    assert snapshot(s) == before


@pytest.mark.parametrize("table", TABLES)
@pytest.mark.parametrize("action", ["update", "delete"])
def test_uncommitted_return_history_cannot_be_rewritten(drafts, table, action):
    s = drafts
    values = rows(s)
    before = snapshot(s)
    with pytest.raises(IntegrityError) as error, s["engine"].begin() as c:
        pending(c, values)
        c.execute(delete(table) if action == "delete" else update(table).values(version=2))
    assert error.value.orig.diag.constraint_name == "ck_business_allocation_return_immutable"
    assert snapshot(s) == before


def test_two_groups_cannot_claim_the_same_original_return_position(drafts):
    s = drafts
    group, member = rows(s)
    another_group = group | {
        "id": uuid4(),
        "idempotency_key": "fictional-payment-" + str(group["payment_id"]),
    }
    another_member = member | {
        "id": uuid4(),
        "return_id": another_group["id"],
        "operation_id": uuid4(),
        "journal_id": uuid4(),
    }
    before = snapshot(s)
    with pytest.raises(IntegrityError) as error, s["engine"].begin() as c:
        pending(c, (group, member))
        pending(c, (another_group, another_member))
    assert (
        error.value.orig.diag.constraint_name == "uq_business_allocation_return_line_source_order"
    )
    assert snapshot(s) == before


@pytest.mark.parametrize(
    "field,value,constraint",
    [
        ("source_allocation_line_id", "foreign", "fk_business_allocation_return_line_source"),
        ("payment_id", "foreign", "fk_business_allocation_return_line_group"),
        ("line_no", 0, "ck_business_allocation_return_line_position"),
        ("line_no", 201, "ck_business_allocation_return_line_position"),
        ("source_version", 0, "ck_business_allocation_return_line_source_version"),
        ("source_return_no", 0, "ck_business_allocation_return_line_source_order"),
        ("payment_amount", Decimal("0"), "ck_business_allocation_return_line_amounts"),
        ("document_amount", Decimal("-1"), "ck_business_allocation_return_line_amounts"),
    ],
)
def test_original_scope_and_quantity_representation_are_enforced(drafts, field, value, constraint):
    s = drafts
    group, member = rows(s)
    member[field] = uuid4() if value == "foreign" else value
    before = snapshot(s)
    with pytest.raises(IntegrityError) as error, s["engine"].begin() as c:
        pending(c, (group, member))
    assert error.value.orig.diag.constraint_name == constraint
    assert snapshot(s) == before


@pytest.mark.parametrize("foreign_reference", ["payment", "original_mapping"])
def test_valid_references_in_another_owned_ledger_cannot_cross_the_return_scope(
    drafts, foreign_reference
):
    s = drafts
    group, member = rows(s)
    entity = s["structure"].create_entity(
        s["owner"],
        EntityCreate(
            kind="personal",
            name="Fictional other return ledger",
            base_asset_id="USD",
            template_key="personal_default",
        ),
    )
    account = s["structure"].create_account(
        s["owner"],
        entity.ledger.id,
        AccountCreate(name="Fictional other bank", kind="bank", asset_ids=["USD"]),
    )
    categories = s["structure"].list_categories(s["owner"], entity.ledger.id)
    other = s | {
        "ledger": entity.ledger.id,
        "account": account,
        "expenses": [item for item in categories if item.kind == "expense"],
    }
    payment, advance = payment_source(other, fee_count=0)
    with s["engine"].begin() as c:
        payment_write(c, other, payment, advance)
    group["payment_id"] = payment.id
    if foreign_reference == "original_mapping":
        group.update(
            ledger_id=other["ledger"], idempotency_key="fictional-payment-" + str(payment.id)
        )
        member.update(ledger_id=other["ledger"], payment_id=payment.id)
        constraint = "fk_business_allocation_return_line_source"
    else:
        constraint = "fk_business_allocation_return_payment"
    before = snapshot(s)
    with pytest.raises(IntegrityError) as error, s["engine"].begin() as c:
        pending(c, (group, member))
    assert error.value.orig.diag.constraint_name == constraint
    assert snapshot(s) == before


def test_empty_round_trip_restores_guards_and_historical_notifications_block_downgrade(drafts):
    s = drafts
    migration = allocation_return_storage_migration()
    with s["engine"].begin() as c, Operations.context(MigrationContext.configure(c)):
        migration["downgrade"]()
        migration["upgrade"]()
    before = snapshot(s)
    with pytest.raises(IntegrityError) as error, s["engine"].begin() as c:
        # Record isolated fictional history through the real trigger; no guard is disabled.
        c.exec_driver_sql(
            "CREATE TEMP TABLE business_allocation_returns(id uuid, ledger_id uuid, version int) "
            "ON COMMIT DROP; CREATE TRIGGER fictional_return_history AFTER INSERT ON "
            "pg_temp.business_allocation_returns FOR EACH ROW EXECUTE FUNCTION "
            "public.coinpup_record_change()"
        )
        c.execute(
            text("INSERT INTO pg_temp.business_allocation_returns VALUES (:id,:ledger,1)"),
            {"id": uuid4(), "ledger": s["ledger"]},
        )
        with Operations.context(MigrationContext.configure(c)):
            migration["downgrade"]()
    assert error.value.orig.diag.constraint_name == "ck_business_allocation_return_downgrade"
    assert snapshot(s) == before
