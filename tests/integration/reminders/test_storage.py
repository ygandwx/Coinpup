"""Fictional reminder history and direct SQL boundaries on disposable PostgreSQL."""

import json
from dataclasses import asdict
from datetime import date
from uuid import uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from coinpup_api.ledger.schemas import EntityCreate
from coinpup_api.reminders.evaluation import ReminderInputs, ReminderRule, evaluate
from coinpup_api.reminders.models import ReminderEvent as Event
from coinpup_api.reminders.models import ReminderEventRevision as Revision
from coinpup_api.sync.locking import acquire_write_lock
from coinpup_api.sync.service import ChangeService
from sqlalchemy import delete, insert, select, update
from sqlalchemy.exc import IntegrityError

from tests.integration.conftest import reminder_migration
from tests.integration.ledger.test_account_classes import snapshot
from tests.integration.ledger.test_posting_service_database import financial_counts
from tests.integration.ledger.test_posting_service_database import ledger_setup as ledger_setup

pytestmark = pytest.mark.integration


def evaluated(offset=30, version="R-v1"):
    rule = ReminderRule(
        "test.fictional.anniversary",
        version,
        date(2026, 10, 10),
        ("Fictional test rule, not legislation",),
        "days_after",
        ("anchor_date",),
        offset_days=offset,
    )
    result = evaluate(
        rule, ReminderInputs(anchor_date=date(2026, 1, 1), applicability_confirmed=True)
    )
    return dict(
        evaluation=json.loads(json.dumps(asdict(result), default=str)),
        evaluation_status=result.status,
        calculated_date=result.calculated_date,
    )


def create(s, **changes):
    values = dict(
        id=uuid4(),
        ledger_id=s["ledger"],
        event_kind="annual",
        title="Fictional 年审提醒",
        last_action="create",
        last_actor_id=s["owner"],
        **evaluated(),
    )
    with s["engine"].begin() as c:
        acquire_write_lock(c)
        c.execute(insert(Event).values(values | changes))
    return values["id"]


def edit(s, identifier, action, **changes):
    with s["engine"].begin() as c:
        acquire_write_lock(c)
        c.execute(
            update(Event)
            .where(Event.id == identifier)
            .values(dict(version=Event.version + 1, last_action=action, last_reason=None) | changes)
        )


def test_override_recalculation_and_completion_preserve_all_versions(ledger_setup):
    s = ledger_setup
    before = financial_counts(s["engine"])
    identifier = create(s)
    edit(
        s,
        identifier,
        "set_manual",
        manual_due_date=date(2026, 2, 10),
        manual_reason="Fictional accountant choice",
        last_reason="Fictional accountant choice",
    )
    edit(s, identifier, "complete", completed=True)
    edit(s, identifier, "recalculate", **evaluated(45, "R-v2"))
    edit(s, identifier, "archive", archived=True)
    edit(s, identifier, "restore", archived=False)
    with s["engine"].connect() as c:
        state = c.execute(select(Event.__table__)).mappings().one()
        history = c.execute(select(Revision.__table__).order_by(Revision.version)).mappings().all()
    assert state["version"] == 6 and state["completed"] and not state["archived"]
    assert state["manual_due_date"] == date(2026, 2, 10)
    assert state["calculated_date"] == date(2026, 2, 15)
    assert [row["version"] for row in history] == list(range(1, 7))
    assert history[0]["snapshot"]["calculated_date"] == "2026-01-31"
    assert history[0]["snapshot"]["manual_due_date"] is None
    assert history[1]["snapshot"]["manual_reason"] == "Fictional accountant choice"
    assert history[3]["snapshot"]["evaluation"]["rule"]["version"] == "R-v2"
    assert financial_counts(s["engine"]) == before
    changes = ChangeService(s["engine"]).list_changes(s["owner"], limit=200).changes
    reminders = [item for item in changes if item.entity_type.startswith("reminder_")]
    assert len(reminders) == 12
    assert {item.entity_type for item in reminders} == {
        "reminder_events",
        "reminder_event_revisions",
    }
    edit(
        s,
        identifier,
        "clear_manual",
        manual_due_date=None,
        manual_reason=None,
        last_reason="Fictional return to computed date",
    )
    with s["engine"].connect() as c:
        assert c.scalar(select(Event.manual_due_date)) is None
        assert c.scalar(select(Event.calculated_date)) == date(2026, 2, 15)


@pytest.mark.parametrize(
    "changes",
    [
        {"manual_due_date": date(2026, 2, 10)},
        {"manual_reason": "Fictional orphan reason"},
        {"manual_due_date": date(2026, 2, 10), "manual_reason": " "},
        {"evaluation": {}},
        {"evaluation_status": "calculated", "calculated_date": None},
        {"calculated_date": date(2026, 2, 1)},
        {"evaluation_status": "needs_verification"},
        {"version": 2},
        {"completed": True},
        {"archived": True},
        {"last_actor_id": uuid4()},
        {"event_kind": "unknown"},
    ],
)
def test_invalid_initial_state_rolls_back_state_audit_and_notifications(ledger_setup, changes):
    before = snapshot(ledger_setup)
    with pytest.raises(IntegrityError):
        create(ledger_setup, **changes)
    assert snapshot(ledger_setup) == before


@pytest.mark.parametrize(
    "action,changes",
    [
        ("edit", {"version": 3}),
        ("edit", {"ledger_id": uuid4()}),
        ("edit", {"event_kind": "tax"}),
        ("edit", {"completed": True}),
        ("complete", {"completed": False}),
        ("reopen", {"completed": False}),
        ("set_manual", {"manual_due_date": date(2026, 2, 10), "manual_reason": "Fictional reason"}),
        ("clear_manual", {"manual_due_date": None, "manual_reason": None}),
        ("create", {}),
    ],
)
def test_invalid_transitions_leave_original_version_and_history(ledger_setup, action, changes):
    s = ledger_setup
    identifier = create(s)
    before = snapshot(s)
    with pytest.raises(IntegrityError):
        edit(s, identifier, action, **changes)
    assert snapshot(s) == before


def test_recalculation_cannot_remove_manual_date_or_reopen_completed_event(ledger_setup):
    s = ledger_setup
    identifier = create(s, manual_due_date=date(2026, 2, 10), manual_reason="Fictional override")
    edit(s, identifier, "complete", completed=True)
    before = snapshot(s)
    for changes in ({"manual_due_date": None, "manual_reason": None}, {"completed": False}):
        with pytest.raises(IntegrityError):
            edit(s, identifier, "recalculate", **(evaluated(45, "R-v2") | changes))
        assert snapshot(s) == before


def test_no_deletion_revision_rewrite_or_forged_future_revision(ledger_setup):
    s = ledger_setup
    identifier = create(s)
    before = snapshot(s)
    statements = [
        delete(Event),
        delete(Revision),
        update(Revision).values(snapshot={}),
        insert(Revision).values(event_id=identifier, ledger_id=s["ledger"], version=2, snapshot={}),
    ]
    for statement in statements:
        with pytest.raises(IntegrityError):
            with s["engine"].begin() as c:
                acquire_write_lock(c)
                c.execute(statement)
        assert snapshot(s) == before


@pytest.mark.parametrize("history", ["rows", "logs"])
def test_empty_round_trip_and_protected_downgrade(ledger_setup, history):
    s = ledger_setup
    migration = reminder_migration()
    with s["engine"].begin() as c, Operations.context(MigrationContext.configure(c)):
        migration["downgrade"]()
        migration["upgrade"]()
    create(s)
    with s["engine"].begin() as c:
        c.exec_driver_sql(
            "TRUNCATE TABLE "
            + ("change_log" if history == "rows" else "reminder_event_revisions, reminder_events")
            + " RESTRICT"
        )
    before = snapshot(s)
    with pytest.raises(IntegrityError) as failure:
        with s["engine"].begin() as c, Operations.context(MigrationContext.configure(c)):
            migration["downgrade"]()
    assert failure.value.orig.diag.constraint_name == "ck_reminder_downgrade"
    assert snapshot(s) == before


def test_manual_only_pending_and_invalid_json_date_are_distinct(ledger_setup):
    s = ledger_setup
    create(
        s,
        evaluation=None,
        evaluation_status="missing_parameters",
        calculated_date=None,
        manual_due_date=date(2026, 2, 10),
        manual_reason="Fictional manual source",
    )
    bad = evaluated()
    bad["evaluation"]["rule"]["checked_on"] = "2026-02-30"
    before = snapshot(s)
    with pytest.raises(IntegrityError):
        create(s, **bad)
    assert snapshot(s) == before


def test_revision_cannot_reference_another_ledger_even_with_matching_snapshot(ledger_setup):
    s = ledger_setup
    identifier = create(s)
    other = s["structure"].create_entity(
        s["owner"],
        EntityCreate(
            kind="personal",
            name="Fictional other reminder ledger",
            base_asset_id="USD",
            template_key="personal_default",
        ),
    )
    with s["engine"].connect() as c:
        proof = c.scalar(select(Revision.snapshot).where(Revision.event_id == identifier))
    before = snapshot(s)
    with pytest.raises(IntegrityError) as failure:
        with s["engine"].begin() as c:
            acquire_write_lock(c)
            c.execute(
                insert(Revision).values(
                    event_id=identifier, ledger_id=other.ledger.id, version=1, snapshot=proof
                )
            )
    assert failure.value.orig.diag.constraint_name == "ck_reminder_revision_history"
    assert snapshot(s) == before
