"""Period histories commit atomically and protect dates without editing old facts."""

from datetime import date
from uuid import uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from coinpup_api.ledger.models import Journal
from coinpup_api.ledger.period_models import LedgerPeriod, PeriodAudit, PeriodReceipt
from coinpup_api.sync.models import ChangeLog
from sqlalchemy import delete, insert, select, update
from sqlalchemy.exc import IntegrityError

from tests.integration.conftest import (
    draft_line_migration,
    period_migration,
    project_dimension_migration,
    project_migration,
)
from tests.integration.ledger.test_account_classes import snapshot
from tests.integration.ledger.test_posting_service_database import classified
from tests.integration.ledger.test_posting_service_database import ledger_setup as ledger_setup

pytestmark = pytest.mark.integration
MODELS = (LedgerPeriod, PeriodAudit, PeriodReceipt)
CUTOFF = date(2026, 1, 31)


def write_period(
    connection,
    s,
    *,
    state_id=None,
    version=2,
    old=None,
    new=CUTOFF,
    action="close",
    omit=None,
    audit_changes=None,
    receipt_changes=None,
):
    state_id = state_id or uuid4()
    audit_id, receipt_id = uuid4(), uuid4()
    if omit != "state":
        if version == 2:
            connection.execute(
                insert(LedgerPeriod).values(
                    id=state_id, ledger_id=s["ledger"], version=version, closed_through=new
                )
            )
        else:
            connection.execute(
                update(LedgerPeriod)
                .where(LedgerPeriod.id == state_id)
                .values(version=version, closed_through=new)
            )
    if omit != "audit":
        connection.execute(
            insert(PeriodAudit).values(
                dict(
                    id=audit_id,
                    ledger_id=s["ledger"],
                    version=version,
                    period_id=state_id,
                    actor_id=s["owner"],
                    action=action,
                    reason="Fictional period test",
                    previous_closed_through=old,
                    closed_through=new,
                )
                | (audit_changes or {})
            )
        )
    if omit != "receipt":
        connection.execute(
            insert(PeriodReceipt).values(
                dict(
                    id=receipt_id,
                    ledger_id=s["ledger"],
                    version=1,
                    audit_id=audit_id,
                    idempotency_key=str(uuid4()),
                    hash_version=2,
                    request_hash="a" * 64,
                    response={"fixture": "Fictional schema probe"},
                )
                | (receipt_changes or {})
            )
        )
    return state_id, audit_id, receipt_id


def test_close_reopen_reclose_records_ordered_immutable_changes(ledger_setup):
    s = ledger_setup
    before = snapshot(s)
    with s["engine"].begin() as c:
        ids = write_period(c, s)
    with s["engine"].begin() as c:
        write_period(c, s, state_id=ids[0], version=3, old=CUTOFF, new=None, action="reopen")
    with s["engine"].begin() as c:
        write_period(c, s, state_id=ids[0], version=4)
    with s["engine"].connect() as c:
        assert c.execute(
            select(PeriodAudit.version).order_by(PeriodAudit.version)
        ).scalars().all() == [2, 3, 4]
        assert c.execute(
            select(PeriodAudit.action).order_by(PeriodAudit.version)
        ).scalars().all() == ["close", "reopen", "close"]
        changes = c.execute(
            select(ChangeLog.entity_type, ChangeLog.entity_version).where(
                ChangeLog.entity_type.in_([m.__tablename__ for m in MODELS])
            )
        ).all()
        assert len(changes) == 9
        assert ("ledger_periods", 4) in changes
    after = snapshot(s)
    assert all(
        after[name] == rows
        for name, rows in before.items()
        if name not in {"change_log", *(m.__tablename__ for m in MODELS)}
    )


@pytest.mark.parametrize("omit", ["state", "audit", "receipt"])
def test_incomplete_period_transaction_rolls_back_every_table(ledger_setup, omit):
    s = ledger_setup
    before = snapshot(s)
    with pytest.raises(IntegrityError):
        with s["engine"].begin() as c:
            write_period(c, s, omit=omit)
    assert snapshot(s) == before


@pytest.mark.parametrize("model", MODELS)
@pytest.mark.parametrize("mutation", ["delete", "update"])
def test_period_identity_audit_and_receipt_cannot_be_rewritten(ledger_setup, model, mutation):
    s = ledger_setup
    with s["engine"].begin() as c:
        write_period(c, s)
    before = snapshot(s)
    with pytest.raises(IntegrityError) as failure:
        with s["engine"].begin() as c:
            c.execute(delete(model) if mutation == "delete" else update(model).values(id=uuid4()))
    assert failure.value.orig.diag.constraint_name == "ck_period_identity"
    assert snapshot(s) == before


@pytest.mark.parametrize(
    "changes",
    [
        {"reason": " "},
        {"actor_id": uuid4()},
        {"ledger_id": uuid4()},
        {"version": 3},
        {"previous_closed_through": CUTOFF, "closed_through": None},
        {"action": "reopen"},
        {"closed_through": date(2026, 2, 1)},
    ],
)
def test_audit_scope_transition_and_chain_are_enforced(ledger_setup, changes):
    s = ledger_setup
    before = snapshot(s)
    with pytest.raises(IntegrityError):
        with s["engine"].begin() as c:
            write_period(c, s, audit_changes=changes)
    assert snapshot(s) == before


@pytest.mark.parametrize(
    "changes",
    [
        {"hash_version": 1},
        {"version": 2},
        {"idempotency_key": " "},
        {"request_hash": "no"},
        {"response": []},
        {"ledger_id": uuid4()},
    ],
)
def test_period_receipt_scope_and_v2_shape(ledger_setup, changes):
    s = ledger_setup
    before = snapshot(s)
    with pytest.raises(IntegrityError):
        with s["engine"].begin() as c:
            write_period(c, s, receipt_changes=changes)
    assert snapshot(s) == before


@pytest.mark.parametrize("which", ["transaction_date", "recognition_date"])
@pytest.mark.parametrize("day", [date(2026, 1, 30), CUTOFF])
def test_direct_journal_guard_covers_either_inclusive_date(ledger_setup, which, day):
    s = ledger_setup
    receipt = s["posting"].post_expense(s["owner"], s["ledger"], classified(s), "before-close")
    with s["engine"].begin() as c:
        original = dict(c.execute(select(Journal)).mappings().one())
        write_period(c, s)
    before = snapshot(s)
    with pytest.raises(IntegrityError) as failure:
        with s["engine"].begin() as c:
            c.execute(
                insert(Journal).values(
                    original
                    | {
                        "id": uuid4(),
                        "transaction_date": date(2026, 2, 1),
                        "recognition_date": date(2026, 2, 1),
                        which: day,
                    }
                )
            )
    assert failure.value.orig.diag.constraint_name == "ck_journal_period_closed"
    assert (
        s["posting"].post_expense(s["owner"], s["ledger"], classified(s), "before-close") == receipt
    )
    assert snapshot(s) == before


@pytest.mark.parametrize("history", ["empty", "rows", "logs"])
def test_period_migration_refuses_history_and_round_trips_empty(ledger_setup, history):
    s = ledger_setup
    if history != "empty":
        with s["engine"].begin() as c:
            write_period(c, s)
        with s["engine"].begin() as c:
            c.exec_driver_sql(
                "TRUNCATE TABLE "
                + (
                    "change_log"
                    if history == "rows"
                    else "ledger_period_receipts, ledger_period_audits, ledger_periods"
                )
                + " RESTRICT"
            )
    before = snapshot(s)

    def run():
        with s["engine"].begin() as c:
            with Operations.context(MigrationContext.configure(c)):
                draft_line_migration()["downgrade"]()
                project_dimension_migration()["downgrade"]()
                project_migration()["downgrade"]()
                period_migration()["downgrade"]()
                period_migration()["upgrade"]()
                project_migration()["upgrade"]()
                project_dimension_migration()["upgrade"]()
                draft_line_migration()["upgrade"]()

    if history == "empty":
        run()
    else:
        with pytest.raises(IntegrityError) as failure:
            run()
        assert failure.value.orig.diag.constraint_name == "ck_period_downgrade"
    assert snapshot(s) == before
