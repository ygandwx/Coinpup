"""Fictional storage probes: no settlement can commit before dedicated kinds activate."""

from datetime import date
from decimal import Decimal
from uuid import uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from coinpup_api.business.models import BusinessParty
from coinpup_api.business.settlement_models import (
    BusinessAllocation,
    BusinessAllocationLine,
    BusinessPayment,
)
from coinpup_api.sync.models import ChangeLog
from sqlalchemy import delete, insert, select, text, update
from sqlalchemy.exc import IntegrityError

from tests.integration.business.test_draft_lines import document, line, save
from tests.integration.conftest import business_settlement_migration
from tests.integration.ledger.test_account_classes import snapshot
from tests.integration.ledger.test_posting_service_database import (
    ledger_setup as ledger_setup,
)
from tests.integration.ledger.test_posting_service_database import (
    opening,
)

pytestmark = pytest.mark.integration
TABLES = (BusinessPayment, BusinessAllocation, BusinessAllocationLine)


def source(s):
    receipt = s["posting"].post_opening(s["owner"], s["ledger"], opening(s), "fictional-storage")
    party = uuid4()
    with s["engine"].begin() as c:
        c.execute(
            insert(BusinessParty).values(
                id=party, ledger_id=s["ledger"], name="Fictional storage supplier", role="supplier"
            )
        )
    parent = document(s)
    original = line(s, parent)
    save(s, original)
    payment_id, group_id = uuid4(), uuid4()
    payment = dict(
        id=payment_id,
        ledger_id=s["ledger"],
        party_id=party,
        account_id=s["account"].id,
        asset_id="USD",
        direction="payout",
        amount=Decimal("1"),
        transaction_date=date(2026, 9, 15),
        description="Fictional storage only",
        fees=[],
        operation_id=receipt.id,
        journal_id=receipt.journal_id,
        initial_allocation_id=None,
        created_by=s["owner"],
        idempotency_key="fictional-storage",
    )
    group = dict(
        id=group_id,
        ledger_id=s["ledger"],
        payment_id=payment_id,
        allocation_date=date(2026, 9, 30),
        created_by=s["owner"],
        idempotency_key="fictional-storage",
    )
    member = dict(
        id=uuid4(),
        allocation_id=group_id,
        payment_id=payment_id,
        ledger_id=s["ledger"],
        document_id=parent,
        document_line_id=original["id"],
        line_no=1,
        payment_amount=Decimal("1"),
        document_amount=Decimal("1"),
        operation_id=uuid4(),
        journal_id=uuid4(),
    )
    return payment, group, member


def pending(c, rows):
    for table, values in zip(TABLES, rows, strict=True):
        c.execute(insert(table).values(**values))


def test_storage_notifications_are_atomic_and_preserve_original_quantities(ledger_setup):
    s = ledger_setup
    rows = source(s)
    before = snapshot(s)
    with s["engine"].connect() as c:
        transaction = c.begin()
        try:
            pending(c, rows)
            assert set(
                c.execute(
                    select(ChangeLog.entity_type, ChangeLog.entity_id).where(
                        ChangeLog.entity_type.in_([table.__tablename__ for table in TABLES])
                    )
                ).all()
            ) == {
                (table.__tablename__, str(row["id"]))
                for table, row in zip(TABLES, rows, strict=True)
            }
            assert c.scalar(select(BusinessPayment.amount)) == Decimal("1")
        finally:
            transaction.rollback()
    assert snapshot(s) == before


def test_legacy_posting_cannot_be_relabelled_as_a_payment(ledger_setup):
    s = ledger_setup
    payment, _, _ = source(s)
    before = snapshot(s)
    with pytest.raises(IntegrityError) as error, s["engine"].begin() as c:
        c.execute(insert(BusinessPayment).values(**payment))
    assert error.value.orig.diag.constraint_name == "ck_business_payment_shape"
    assert snapshot(s) == before


@pytest.mark.parametrize("include_line", [False, True])
def test_empty_or_unbacked_group_requires_dedicated_members(ledger_setup, include_line):
    s = ledger_setup
    payment, group, member = source(s)
    before = snapshot(s)
    with pytest.raises(IntegrityError) as error, s["engine"].begin() as c:
        c.execute(insert(BusinessPayment).values(**payment))
        c.execute(insert(BusinessAllocation).values(**group))
        if include_line:
            c.execute(insert(BusinessAllocationLine).values(**member))
        c.execute(text("SELECT coinpup_validate_business_allocation(:id)"), {"id": group["id"]})
    assert error.value.orig.diag.constraint_name == "ck_business_allocation_shape"
    assert snapshot(s) == before


@pytest.mark.parametrize("table", TABLES)
@pytest.mark.parametrize("action", ["update", "delete"])
def test_even_uncommitted_settlement_facts_cannot_be_rewritten(ledger_setup, table, action):
    s = ledger_setup
    rows = source(s)
    before = snapshot(s)
    with pytest.raises(IntegrityError) as error, s["engine"].begin() as c:
        pending(c, rows)
        c.execute(delete(table) if action == "delete" else update(table).values(version=2))
    assert error.value.orig.diag.constraint_name == "ck_business_settlement_immutable"
    assert snapshot(s) == before


@pytest.mark.parametrize(
    "field,value,constraint",
    [
        ("amount", Decimal("0"), "ck_business_payment_amount"),
        ("amount", Decimal("-1"), "ck_business_payment_amount"),
        ("amount", Decimal("NaN"), "ck_business_payment_amount"),
        ("direction", "transfer", "ck_business_payment_direction"),
        ("fees", {}, "ck_business_payment_fees"),
        ("fees", [{}] * 21, "ck_business_payment_fees"),
        ("version", 2, "ck_business_payment_version"),
        ("created_by", "foreign", "fk_business_payment_owner"),
        ("party_id", "foreign", "fk_business_payment_party"),
        ("account_id", "foreign", "fk_business_payment_account_asset"),
    ],
)
def test_payment_scope_and_representation_are_checked_before_commit(
    ledger_setup, field, value, constraint
):
    s = ledger_setup
    payment, _, _ = source(s)
    before = snapshot(s)
    payment[field] = uuid4() if value == "foreign" else value
    with pytest.raises(IntegrityError) as error, s["engine"].begin() as c:
        c.execute(insert(BusinessPayment).values(**payment))
    assert error.value.orig.diag.constraint_name == constraint
    assert snapshot(s) == before


@pytest.mark.parametrize(
    "field,value,constraint",
    [
        ("payment_id", "foreign", "fk_business_allocation_line_group"),
        ("document_line_id", "foreign", "fk_business_allocation_line_source"),
        ("line_no", 0, "ck_business_allocation_line_position"),
        ("line_no", 201, "ck_business_allocation_line_position"),
        ("payment_amount", Decimal("0"), "ck_business_allocation_line_amounts"),
        ("document_amount", Decimal("NaN"), "ck_business_allocation_line_amounts"),
    ],
)
def test_line_scope_and_positive_bounded_quantities(ledger_setup, field, value, constraint):
    s = ledger_setup
    payment, group, member = source(s)
    before = snapshot(s)
    member[field] = uuid4() if value == "foreign" else value
    with pytest.raises(IntegrityError) as error, s["engine"].begin() as c:
        pending(c, (payment, group, member))
    assert error.value.orig.diag.constraint_name == constraint
    assert snapshot(s) == before


def test_initial_allocation_is_a_deferred_same_payment_reference(ledger_setup):
    s = ledger_setup
    payment, group, _ = source(s)
    payment["initial_allocation_id"] = group["id"]
    before = snapshot(s)
    with pytest.raises(IntegrityError) as error, s["engine"].begin() as c:
        c.execute(insert(BusinessPayment).values(**payment))
        c.execute(text("SET CONSTRAINTS fk_business_payment_initial_allocation IMMEDIATE"))
    assert error.value.orig.diag.constraint_name == "fk_business_payment_initial_allocation"
    assert snapshot(s) == before


def test_empty_migration_round_trip_and_history_downgrade_guards(ledger_setup):
    s = ledger_setup
    migration = business_settlement_migration()
    with s["engine"].begin() as c, Operations.context(MigrationContext.configure(c)):
        migration["downgrade"]()
        migration["upgrade"]()
    payment, _, _ = source(s)
    before = snapshot(s)
    for history in ("row", "notification"):
        with pytest.raises(IntegrityError) as error, s["engine"].begin() as c:
            if history == "row":
                c.execute(insert(BusinessPayment).values(**payment))
            else:
                c.execute(text("SELECT pg_advisory_xact_lock(18945999704708432)"))
                c.execute(
                    insert(ChangeLog).values(
                        owner_id=s["owner"],
                        ledger_id=s["ledger"],
                        entity_type="business_payments",
                        entity_id=str(uuid4()),
                        entity_version=1,
                        change_kind="upsert",
                    )
                )
            with Operations.context(MigrationContext.configure(c)):
                business_settlement_migration(include_dependents=False)["downgrade"]()
        assert error.value.orig.diag.constraint_name == "ck_business_settlement_downgrade"
        assert snapshot(s) == before
