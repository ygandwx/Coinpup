"""Fictional allocations, immutable dimensions, capacity and concurrent commit checks."""

from concurrent.futures import ThreadPoolExecutor
from datetime import date
from decimal import Decimal
from threading import Event
from uuid import UUID, uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from coinpup_api.business.settlement_models import BusinessAllocationLine
from coinpup_api.ledger.models import CommandReceipt, JournalLine
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError

from tests.integration.business.allocation_fixture import source, write
from tests.integration.business.payment_fixture import write as payment_write
from tests.integration.business.test_confirmation_service import prepare, submit
from tests.integration.business.test_draft_service import drafts as drafts
from tests.integration.business.test_payment_activation import balance
from tests.integration.conftest import business_allocation_migration
from tests.integration.ledger.test_account_classes import snapshot
from tests.integration.ledger.test_period_transactions import change as change_period
from tests.integration.ledger.test_posting_service_database import ledger_setup as ledger_setup

pytestmark = pytest.mark.integration


def reject(s, facts, **changes):
    before = snapshot(s)
    with pytest.raises(IntegrityError) as error, s["engine"].begin() as c:
        write(c, s, *facts, **changes)
    assert error.value.orig.diag.constraint_name == "ck_business_allocation_shape"
    assert snapshot(s) == before


@pytest.mark.parametrize("direction", ["receipt", "payout"])
@pytest.mark.parametrize("asset", ["USD", "EUR"])
def test_complete_clearing_zeroes_each_original_dimension_without_more_cash(
    drafts, direction, asset
):
    s = drafts
    facts = source(s, direction, document_asset=asset)
    with s["engine"].connect() as c:
        bank = balance(c, s["account"].id, "USD")
        expense = c.execute(
            select(JournalLine.asset_id, func.sum(JournalLine.amount))
            .where(JournalLine.role.in_(["income", "expense"]))
            .group_by(JournalLine.asset_id)
        ).all()
    with s["engine"].begin() as c:
        result = write(c, s, *facts)
    with s["engine"].connect() as c:
        assert balance(c, s["account"].id, "USD") == bank
        assert (
            c.execute(
                select(JournalLine.asset_id, func.sum(JournalLine.amount))
                .where(JournalLine.role.in_(["income", "expense"]))
                .group_by(JournalLine.asset_id)
            ).all()
            == expense
        )
        groups = (
            c.execute(
                select(func.sum(JournalLine.amount))
                .where(JournalLine.account_id.in_([facts[1], facts[3]]))
                .group_by(
                    JournalLine.account_id,
                    JournalLine.asset_id,
                    JournalLine.party_id,
                    JournalLine.document_id,
                    JournalLine.document_line_id,
                    JournalLine.project_id,
                )
            )
            .scalars()
            .all()
        )
        assert groups and all(amount == 0 for amount in groups)
        assert (
            c.scalar(
                select(CommandReceipt.response).where(
                    CommandReceipt.operation_id == UUID(result["postings"][0]["operation_id"])
                )
            )
            == result
        )


@pytest.mark.parametrize("initial", [False, True])
def test_initial_or_later_partial_groups_keep_exact_remaining_capacity(drafts, initial):
    s = drafts
    facts = source(s, committed=not initial)
    with s["engine"].begin() as c:
        first = write(c, s, *facts, payment_amount="40", document_amount="40", initial=initial)
    with s["engine"].begin() as c:
        write(c, s, *facts, payment_amount="70", document_amount="70")
    with s["engine"].connect() as c:
        assert c.scalar(select(func.sum(BusinessAllocationLine.payment_amount))) == Decimal("110")
        if initial:
            saved = c.scalar(
                select(CommandReceipt.response).where(
                    CommandReceipt.key == "fictional-payment-" + str(facts[0].id)
                )
            )
            assert saved["initial_allocation"] == first
    reject(s, facts, payment_amount="0.01", document_amount="0.01")


@pytest.mark.parametrize(
    "changes",
    [
        {"group_changes": {"allocation_date": date(2026, 9, 14)}},
        {"group_changes": {"allocation_date": date(2026, 9, 29)}},
        {"header_changes": {"recognition_date": date(2026, 10, 1)}},
        {"header_changes": {"description": "Fictional different source"}},
        {"line_changes": {"line_no": 2}},
        {"line_changes": {"payment_amount": Decimal("110.01")}},
        {"line_changes": {"document_amount": Decimal("110.01")}},
        {"line_changes": {"payment_amount": Decimal("109.001")}},
        {"line_changes": {"document_amount": Decimal("109.001")}},
        {"line_changes": {"document_amount": Decimal("109")}},
        {"receipt_changes": {"hash_version": 1}},
        {"receipt_changes": {"response_status": 200}},
        {"receipt_changes": {"response": {}}},
        {"omit_line": True},
    ],
)
def test_incomplete_or_inconsistent_groups_roll_back_every_table(drafts, changes):
    reject(drafts, source(drafts), **changes)


@pytest.mark.parametrize(
    "fault", ["sign", "amount", "advance_bucket", "source_project", "exchange_bucket"]
)
def test_balanced_wrong_shapes_cannot_move_or_rebucket_old_debt(drafts, fault):
    facts = source(drafts, document_asset="EUR")

    def change(rows):
        if fault in {"sign", "amount"}:
            for row in rows:
                row["amount"] *= -1 if fault == "sign" else 2
        elif fault == "advance_bucket":
            for index in (0, 2):
                rows[index].update(
                    document_id=facts[2].id,
                    document_line_id=facts[2].lines[0].id,
                    project_id=facts[2].lines[0].project_id,
                )
        elif fault == "source_project":
            rows[1]["project_id"] = rows[3]["project_id"] = None
        else:
            rows[2]["document_id"] = facts[2].id

    reject(drafts, facts, change_rows=change)


@pytest.mark.parametrize("isolation", ["REPEATABLE READ", "SERIALIZABLE"])
def test_snapshot_isolation_is_rejected_before_allocation_notification(drafts, isolation):
    s = drafts
    facts = source(s)
    before = snapshot(s)
    with pytest.raises(IntegrityError) as error, s["engine"].begin() as c:
        c.exec_driver_sql("SET TRANSACTION ISOLATION LEVEL " + isolation)
        write(c, s, *facts)
    assert error.value.orig.diag.constraint_name == "ck_business_allocation_isolation"
    assert snapshot(s) == before


@pytest.mark.parametrize("capacity", ["payment", "source"])
def test_old_read_before_waiting_lock_cannot_overallocate_at_commit(drafts, capacity):
    s = drafts
    facts = source(s)
    payment, advance, document, control = facts
    if capacity == "source":
        payment = payment.model_copy(update={"id": uuid4()})
        with s["engine"].begin() as c:
            payment_write(c, s, payment, advance)
    else:
        document, request = prepare(
            s, kind="bill", changes={"party_id": payment.party_id, "issue_date": "2026-09-30"}
        )
        submit(s, document, request, "fictional-second-confirm")
    held, stale_read = Event(), Event()

    def first():
        with s["engine"].begin() as c:
            write(c, s, *facts, payment_amount="60", document_amount="60")
            held.set()
            assert stale_read.wait(10)

    def second():
        assert held.wait(10)
        with pytest.raises(IntegrityError) as error, s["engine"].begin() as c:
            assert c.scalar(select(func.count()).select_from(BusinessAllocationLine)) == 0
            stale_read.set()
            write(
                c, s, payment, advance, document, control, payment_amount="60", document_amount="60"
            )
        assert error.value.orig.diag.constraint_name == "ck_business_allocation_shape"

    with ThreadPoolExecutor(max_workers=2) as pool:
        a, b = pool.submit(first), pool.submit(second)
        a.result(timeout=30)
        b.result(timeout=30)
    with s["engine"].connect() as c:
        assert c.scalar(select(func.sum(BusinessAllocationLine.payment_amount))) == Decimal("60")
        assert c.scalar(select(func.count()).select_from(BusinessAllocationLine)) == 1


def test_closed_original_period_can_clear_only_in_an_open_period(drafts):
    s = drafts
    facts = source(s)
    change_period(s, cutoff="2026-09-30")
    before = snapshot(s)
    with pytest.raises(IntegrityError), s["engine"].begin() as c:
        write(c, s, *facts)
    assert snapshot(s) == before
    with s["engine"].begin() as c:
        write(c, s, *facts, allocation_date="2026-10-01")


def test_activation_roundtrip_preserves_payments_and_refuses_allocation_history(drafts):
    s = drafts
    facts = source(s)
    migration = business_allocation_migration()
    before = snapshot(s)
    with s["engine"].begin() as c, Operations.context(MigrationContext.configure(c)):
        migration["downgrade"]()
        migration["upgrade"]()
    assert snapshot(s) == before
    with s["engine"].begin() as c:
        write(c, s, *facts)
    before = snapshot(s)
    with pytest.raises(IntegrityError) as error, s["engine"].begin() as c:
        with Operations.context(MigrationContext.configure(c)):
            migration["downgrade"]()
    assert error.value.orig.diag.constraint_name == "ck_business_allocation_downgrade"
    assert snapshot(s) == before


def test_notification_history_alone_prevents_allocation_downgrade(drafts):
    s = drafts
    before = snapshot(s)
    with pytest.raises(IntegrityError) as error, s["engine"].begin() as c:
        c.exec_driver_sql(
            "CREATE TEMP TABLE business_allocations(id uuid, ledger_id uuid, version int) "
            "ON COMMIT DROP; CREATE TRIGGER fictional_history AFTER INSERT ON "
            "pg_temp.business_allocations "
            "FOR EACH ROW EXECUTE FUNCTION public.coinpup_record_change()"
        )
        c.execute(
            text("INSERT INTO pg_temp.business_allocations VALUES (:id,:ledger,1)"),
            {"id": uuid4(), "ledger": s["ledger"]},
        )
        with Operations.context(MigrationContext.configure(c)):
            business_allocation_migration()["downgrade"]()
    assert error.value.orig.diag.constraint_name == "ck_business_allocation_downgrade"
    assert snapshot(s) == before
