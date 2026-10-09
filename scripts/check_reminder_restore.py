"""Fictional manual date, recalculation and completion survive the full bundle."""

import json
from dataclasses import asdict
from datetime import date
from uuid import uuid4

from coinpup_api.ledger.models import Entity, Ledger
from coinpup_api.reminders.catalog import VERSION, get_rule
from coinpup_api.reminders.evaluation import ReminderInputs, evaluate
from coinpup_api.reminders.models import ReminderEvent as Event
from coinpup_api.reminders.models import ReminderEventRevision as Revision
from coinpup_api.sync.locking import acquire_write_lock
from sqlalchemy import insert, select, update


def seed_reminders(engine, owner, ledger):
    identifier = uuid4()
    result = evaluate(
        get_rule("certificate.expiry", VERSION),
        ReminderInputs(expiry_date=date(2026, 1, 31), applicability_confirmed=True),
    )
    proof = json.loads(json.dumps(asdict(result), default=str))
    with engine.begin() as c:
        acquire_write_lock(c)
        entity = c.execute(
            select(Ledger.entity_id)
            .where(Ledger.id == ledger, Ledger.owner_id == owner)
            .with_for_update()
        ).scalar_one()
        c.execute(select(Entity.id).where(Entity.id == entity).with_for_update()).scalar_one()
        c.execute(
            insert(Event).values(
                id=identifier,
                ledger_id=ledger,
                event_kind="certificate",
                title="Fictional 证件提醒",
                evaluation_status="missing_parameters",
                manual_due_date=date(2026, 2, 10),
                manual_reason="Fictional verified individual date",
                last_actor_id=owner,
                last_action="create",
            )
        )
        c.execute(
            update(Event)
            .where(Event.id == identifier)
            .values(
                version=2,
                last_action="recalculate",
                evaluation=proof,
                evaluation_status="calculated",
                calculated_date=date(2026, 1, 31),
            )
        )
        c.execute(
            update(Event)
            .where(Event.id == identifier)
            .values(version=3, last_action="complete", completed=True)
        )
    return identifier


def verify_reminders(engine, identifier):
    with engine.connect() as c:
        row = c.execute(select(Event.__table__).where(Event.id == identifier)).mappings().one()
        history = (
            c.execute(
                select(Revision.__table__)
                .where(Revision.event_id == identifier)
                .order_by(Revision.version)
            )
            .mappings()
            .all()
        )
    assert row["version"] == 3 and row["completed"] and not row["archived"]
    assert row["manual_due_date"] == date(2026, 2, 10)
    assert row["calculated_date"] == date(2026, 1, 31)
    assert [item["version"] for item in history] == [1, 2, 3]
    assert history[0]["snapshot"]["evaluation"] is None
    assert history[1]["snapshot"]["evaluation"]["rule"]["version"] == VERSION
    assert all(item["snapshot"]["manual_due_date"] == "2026-02-10" for item in history)
