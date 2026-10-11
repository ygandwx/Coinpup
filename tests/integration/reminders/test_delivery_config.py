"""Fictional delivery settings: SQL boundaries, stable plans and append-only history."""

from datetime import date, timedelta
from uuid import uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from coinpup_api.ledger.schemas import EntityCreate
from coinpup_api.reminders.delivery_clock import schedule_delivery
from coinpup_api.reminders.delivery_models import CLOCK_FIELDS
from coinpup_api.reminders.delivery_models import ReminderDeliveryConfig as Config
from coinpup_api.reminders.delivery_models import ReminderDeliveryConfigRevision as Revision
from coinpup_api.sync.locking import acquire_write_lock
from coinpup_api.sync.service import ChangeService
from sqlalchemy import delete, insert, select, update
from sqlalchemy.exc import IntegrityError

from tests.integration.conftest import delivery_config_migration
from tests.integration.ledger.test_account_classes import snapshot
from tests.integration.ledger.test_posting_service_database import financial_counts
from tests.integration.ledger.test_posting_service_database import ledger_setup as ledger_setup
from tests.integration.reminders.test_storage import create as create_event

pytestmark = pytest.mark.integration


def plan(due=date(2026, 1, 31), lead=7):
    clock = schedule_delivery(due, lead, "Asia/Shanghai", 540)
    return dict(
        planned_for_date=due,
        planned_at=clock.scheduled_at,
        planned_timezone=clock.timezone,
        planned_minute=clock.requested_minute,
        planned_lead_days=lead,
        resolved_minute=clock.resolved_minute,
        planned_tzdata_version=clock.tzdata_version,
    )


def values(s, event):
    return dict(
        id=uuid4(),
        ledger_id=s["ledger"],
        event_id=event,
        enabled=True,
        lead_days=7,
        timezone="Asia/Shanghai",
        minute=540,
        locale="zh",
        inbox_enabled=True,
        email_enabled=True,
        email_recipient="fictional@example.test",
        last_actor_id=s["owner"],
        last_action="create",
        **plan(),
    )


def write(s, statement):
    with s["engine"].begin() as c:
        acquire_write_lock(c)
        c.execute(statement)


def create(s, event, **changes):
    payload = values(s, event) | changes
    write(s, insert(Config).values(payload))
    return payload["id"]


def edit(s, identifier, action, **changes):
    write(
        s,
        update(Config)
        .where(Config.id == identifier)
        .values(dict(version=Config.version + 1, last_action=action) | changes),
    )


def test_configuration_plan_and_history_are_atomic_and_do_not_touch_financials(ledger_setup):
    s = ledger_setup
    event = create_event(s)
    before = financial_counts(s["engine"])
    identifier = create(s, event)
    edit(s, identifier, "configure", enabled=False, blocked_reason="disabled")
    edit(s, identifier, "configure", enabled=True, blocked_reason=None)
    edit(s, identifier, "configure", lead_days=8, blocked_reason="unplanned")
    edit(s, identifier, "plan", **plan(lead=8), blocked_reason=None)
    with s["engine"].connect() as c:
        row = c.execute(select(Config.__table__)).mappings().one()
        history = c.execute(select(Revision).order_by(Revision.version)).mappings().all()
    assert row["version"] == 5 and row["planned_at"] == plan(lead=8)["planned_at"]
    assert [r["version"] for r in history] == [1, 2, 3, 4, 5]
    assert all(
        history[i]["snapshot"]["planned_at"] == history[0]["snapshot"]["planned_at"]
        for i in (1, 2, 3)
    )
    assert history[3]["snapshot"]["lead_days"] == 8
    assert history[3]["snapshot"]["planned_lead_days"] == 7
    assert history[4]["snapshot"]["planned_lead_days"] == 8
    assert financial_counts(s["engine"]) == before
    changes = ChangeService(s["engine"]).list_changes(s["owner"], limit=200).changes
    notifications = [r for r in changes if r.entity_type.startswith("reminder_delivery_")]
    assert len(notifications) == 10
    assert {r.entity_type for r in notifications} == set(delivery_config_migration()["_TABLES"])


@pytest.mark.parametrize(
    "changes",
    [
        {"version": 2},
        {"last_action": "plan"},
        {"last_actor_id": uuid4()},
        {"lead_days": -1},
        {"lead_days": 3661},
        {"minute": 1440},
        {"locale": "xx"},
        {"timezone": "../UTC"},
        {"timezone": "localtime"},
        {"planned_timezone": "right/UTC"},
        {"inbox_enabled": False, "email_enabled": False},
        {"email_recipient": None},
        {"email_recipient": "fictional@example.test\nBcc: extra@example.test"},
        {"planned_at": None},
        {"planned_tzdata_version": " "},
        {"resolved_minute": 539},
        {"blocked_reason": "disabled"},
        {"enabled": False},
        {"blocked_reason": "invented"},
    ],
)
def test_invalid_configuration_rolls_back_state_history_and_changes(ledger_setup, changes):
    s = ledger_setup
    event = create_event(s)
    before = snapshot(s)
    with pytest.raises(IntegrityError):
        create(s, event, **changes)
    assert snapshot(s) == before


@pytest.mark.parametrize(
    "action,changes",
    [
        ("configure", {"version": 4}),
        ("configure", {"event_id": uuid4()}),
        ("configure", {"ledger_id": uuid4()}),
        ("configure", {"created_at": plan()["planned_at"]}),
        ("create", {}),
        ("plan", {"email_recipient": "other-fictional@example.test"}),
        ("plan", {"planned_tzdata_version": "fictional-new-version"}),
        ("plan", {"planned_at": plan()["planned_at"] + timedelta(hours=1)}),
        ("plan", {"resolved_minute": 541}),
        ("plan", {**dict.fromkeys(CLOCK_FIELDS), "blocked_reason": "missing_date"}),
        ("configure", {**plan(date(2026, 2, 1))}),
    ],
)
def test_identity_and_same_input_plan_cannot_be_rewritten(ledger_setup, action, changes):
    s = ledger_setup
    identifier = create(s, create_event(s))
    before = snapshot(s)
    with pytest.raises(IntegrityError):
        edit(s, identifier, action, **changes)
    assert snapshot(s) == before


def test_pending_date_can_be_planned_and_changed_date_creates_new_snapshot(ledger_setup):
    s = ledger_setup
    identifier = create(
        s, create_event(s), **dict.fromkeys(CLOCK_FIELDS), blocked_reason="missing_date"
    )
    edit(s, identifier, "plan", **plan(), blocked_reason=None)
    edit(s, identifier, "plan", **plan(date(2026, 2, 1)))
    with s["engine"].connect() as c:
        assert c.scalar(select(Config.planned_for_date)) == date(2026, 2, 1)
        assert c.scalars(select(Revision.version).order_by(Revision.version)).all() == [1, 2, 3]


def test_single_config_per_event_and_no_deletion_or_forged_revision(ledger_setup):
    s = ledger_setup
    event = create_event(s)
    identifier = create(s, event)
    before = snapshot(s)
    for statement in [
        insert(Config).values(values(s, event)),
        delete(Config),
        delete(Revision),
        update(Revision).values(snapshot={}),
        insert(Revision).values(
            config_id=identifier, ledger_id=s["ledger"], version=2, snapshot={}
        ),
    ]:
        with pytest.raises(IntegrityError):
            write(s, statement)
        assert snapshot(s) == before


def test_cross_ledger_event_and_revision_are_rejected(ledger_setup):
    s = ledger_setup
    event = create_event(s)
    other = s["structure"].create_entity(
        s["owner"],
        EntityCreate(
            kind="personal",
            name="Fictional other notification ledger",
            base_asset_id="USD",
            template_key="personal_default",
        ),
    )
    before = snapshot(s)
    with pytest.raises(IntegrityError) as error:
        create(s, event, ledger_id=other.ledger.id)
    assert error.value.orig.diag.constraint_name == "fk_reminder_delivery_config_event"
    assert snapshot(s) == before
    identifier = create(s, event)
    with s["engine"].connect() as c:
        proof = c.scalar(select(Revision.snapshot))
    before = snapshot(s)
    with pytest.raises(IntegrityError):
        write(
            s,
            insert(Revision).values(
                config_id=identifier, ledger_id=other.ledger.id, version=1, snapshot=proof
            ),
        )
    assert snapshot(s) == before


@pytest.mark.parametrize("history", ["rows", "logs"])
def test_empty_round_trip_and_guarded_downgrade(ledger_setup, history):
    s = ledger_setup
    migration = delivery_config_migration()
    with s["engine"].begin() as c, Operations.context(MigrationContext.configure(c)):
        migration["downgrade"]()
        migration["upgrade"]()
    create(s, create_event(s))
    with s["engine"].begin() as c:
        target = "change_log" if history == "rows" else ", ".join(reversed(migration["_TABLES"]))
        c.exec_driver_sql("TRUNCATE TABLE " + target + " RESTRICT")
    before = snapshot(s)
    with pytest.raises(IntegrityError) as error:
        with s["engine"].begin() as c, Operations.context(MigrationContext.configure(c)):
            migration["downgrade"]()
    assert error.value.orig.diag.constraint_name == "ck_reminder_delivery_downgrade"
    assert snapshot(s) == before
