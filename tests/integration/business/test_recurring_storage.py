"""Fictional recurring templates/instances exercise actual PostgreSQL commit guards."""

from datetime import date
from uuid import uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from coinpup_api.business.draft_schemas import BusinessDraftCreate, BusinessDraftUpdate
from coinpup_api.business.models import (
    BusinessDocument,
    BusinessDocumentLine,
)
from coinpup_api.business.models import (
    RecurringInvoiceInstance as Instance,
)
from coinpup_api.business.models import (
    RecurringInvoiceRule as Rule,
)
from coinpup_api.business.recurrence_calendar import CalendarSchedule, instantiate_draft
from coinpup_api.ledger.schemas import EntityCreate
from coinpup_api.sync.locking import acquire_write_lock
from coinpup_api.sync.models import ChangeLog
from sqlalchemy import delete, insert, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from tests.integration.business.test_draft_service import drafts as drafts
from tests.integration.conftest import recurring_migration
from tests.integration.ledger.test_account_classes import snapshot
from tests.integration.ledger.test_posting_service_database import financial_counts
from tests.integration.ledger.test_posting_service_database import ledger_setup as ledger_setup

pytestmark = pytest.mark.integration


@pytest.fixture
def recurrence(drafts):
    s = drafts
    body = s["payload"]()
    s["service"].create_draft(s["owner"], s["ledger"], body)
    values = dict(
        id=uuid4(),
        ledger_id=s["ledger"],
        name="Fictional 每月 Invoice",
        timezone_name="Asia/Shanghai",
        anchor_date=date(2026, 1, 31),
        frequency="month",
        interval_count=1,
        source_document_id=body.id,
        source_version=1,
        template_input=body.model_dump(mode="json"),
    )
    return s | dict(source=body, rule=values)


def create_rule(s, **changes):
    with s["engine"].begin() as c:
        c.execute(insert(Rule).values(s["rule"] | changes))


def edit_rule(s, **changes):
    with s["engine"].begin() as c:
        c.execute(
            update(Rule)
            .where(Rule.id == s["rule"]["id"])
            .values(version=Rule.version + 1, **changes)
        )


def generate_fixture(s, *, advance=True, tamper=None):
    # Test-only setup uses the real draft preparation and the production lock order.
    with Session(s["engine"]) as session, session.begin():
        acquire_write_lock(session)
        _, entity = s["service"]._ledger(session, s["owner"], s["ledger"], write=True)
        rule = session.get(Rule, s["rule"]["id"])
        scheduled = CalendarSchedule(
            rule.anchor_date, rule.frequency, rule.interval_count
        ).occurrence(rule.next_index)
        body = instantiate_draft(
            BusinessDraftCreate.model_validate(rule.template_input),
            rule.id,
            rule.next_index,
            scheduled,
        )
        if tamper == "template":
            body = body.model_copy(update={"notes": "Fictional unrequested replacement"})
        header, lines, _ = s["service"]._prepare(session, s["ledger"], entity, body)
        session.add(BusinessDocument(id=body.id, **header))
        session.flush()
        session.add_all(BusinessDocumentLine(document_id=body.id, **line) for line in lines)
        session.flush()
        original = body.model_dump(mode="json")
        if tamper == "original":
            original["notes"] = "Fictional invented receipt"
        session.add(
            Instance(
                id=body.id,
                ledger_id=s["ledger"],
                rule_id=rule.id,
                occurrence_index=rule.next_index + (tamper == "index"),
                rule_version=rule.version + (tamper == "version"),
                scheduled_date=date(2026, 2, 1) if tamper == "date" else scheduled,
                original_input=original,
            )
        )
        session.flush()
        if advance:
            rule.next_index += 1
            rule.version += 1
        return body


def test_capture_refresh_and_generation_preserve_history_without_posting(recurrence):
    s = recurrence
    before = financial_counts(s["engine"])
    create_rule(s)
    changed = s["source"].model_copy(update={"notes": "Fictional explicit new template"})
    s["service"].update_draft(
        s["owner"],
        s["ledger"],
        changed.id,
        BusinessDraftUpdate(expected_version=1, **changed.model_dump(exclude={"id"})),
    )
    with s["engine"].connect() as c:
        assert c.scalar(select(Rule.template_input)) == s["source"].model_dump(mode="json")
    edit_rule(s, source_version=2, template_input=changed.model_dump(mode="json"))
    generated = generate_fixture(s)
    assert generated.issue_date == date(2026, 1, 31)
    assert generated.notes == changed.notes
    s["service"].update_draft(
        s["owner"],
        s["ledger"],
        generated.id,
        BusinessDraftUpdate(
            expected_version=1,
            **(generated.model_dump(exclude={"id"}) | {"notes": "Fictional human edit"}),
        ),
    )
    edit_rule(s, name="Fictional renamed rule")
    with s["engine"].connect() as c:
        assert c.scalar(select(Instance.original_input)) == generated.model_dump(mode="json")
        assert c.scalar(select(Rule.next_index)) == 1
        types = set(c.scalars(select(ChangeLog.entity_type)))
        assert {"recurring_invoice_rules", "recurring_invoice_instances"} <= types
    assert financial_counts(s["engine"]) == before


@pytest.mark.parametrize(
    "change",
    [
        {"name": " "},
        {"timezone_name": "Fictional/Unavailable"},
        {"frequency": "hour"},
        {"interval_count": 0},
        {"interval_count": 121},
        {"next_index": 1},
        {"source_version": 99},
        {"template_input": {}},
        {"source_document_id": uuid4()},
    ],
)
def test_invalid_rule_capture_is_atomic(recurrence, change):
    before = snapshot(recurrence)
    with pytest.raises(IntegrityError):
        create_rule(recurrence, **change)
    assert snapshot(recurrence) == before


def test_cross_ledger_and_embedded_reference_forgery_rejected(recurrence):
    s = recurrence
    other = s["structure"].create_entity(
        s["owner"],
        EntityCreate(
            kind="personal", name="Fictional foreign recurring ledger", base_asset_id="USD"
        ),
    )
    forged = s["source"].model_dump(mode="json")
    forged["lines"][0]["category_id"] = str(uuid4())
    before = snapshot(s)
    for change in ({"ledger_id": other.ledger.id}, {"template_input": forged}):
        with pytest.raises(IntegrityError) as failure:
            create_rule(s, **change)
        assert failure.value.orig.diag.constraint_name == "ck_recurring_rule_template"
        assert snapshot(s) == before


@pytest.mark.parametrize(
    "change",
    [
        {"anchor_date": date(2026, 2, 1)},
        {"timezone_name": "UTC"},
        {"frequency": "year"},
        {"interval_count": 2},
        {"next_index": -1},
        {"id": uuid4()},
        {"ledger_id": uuid4()},
    ],
)
def test_rule_schedule_and_identity_are_immutable(recurrence, change):
    create_rule(recurrence)
    before = snapshot(recurrence)
    with pytest.raises(IntegrityError) as failure:
        edit_rule(recurrence, **change)
    assert failure.value.orig.diag.constraint_name == "ck_recurring_rule_identity"
    assert snapshot(recurrence) == before


@pytest.mark.parametrize("tamper", ["template", "original", "index", "version", "date"])
def test_instance_mismatch_rolls_back_draft_lines_and_notifications(recurrence, tamper):
    create_rule(recurrence)
    before = snapshot(recurrence)
    with pytest.raises(IntegrityError):
        generate_fixture(recurrence, tamper=tamper)
    assert snapshot(recurrence) == before


@pytest.mark.parametrize("side", ["cursor_only", "instance_only"])
def test_deferred_progress_requires_both_halves(recurrence, side):
    create_rule(recurrence)
    before = snapshot(recurrence)
    with pytest.raises(IntegrityError) as failure:
        if side == "cursor_only":
            edit_rule(recurrence, next_index=1)
        else:
            generate_fixture(recurrence, advance=False)
    assert failure.value.orig.diag.constraint_name == "ck_recurring_progress"
    assert snapshot(recurrence) == before


def test_archival_and_immutable_instances(recurrence):
    s = recurrence
    create_rule(s)
    edit_rule(s, archived=True)
    before = snapshot(s)
    with pytest.raises(IntegrityError):
        generate_fixture(s)
    assert snapshot(s) == before
    edit_rule(s, archived=False)
    generate_fixture(s)
    before = snapshot(s)
    for statement in (delete(Rule), delete(Instance), update(Instance).values(version=2)):
        with pytest.raises(IntegrityError):
            with s["engine"].begin() as c:
                c.execute(statement)
        assert snapshot(s) == before


@pytest.mark.parametrize(
    "anchor,frequency,interval,index",
    [
        (date(2026, 1, 31), "month", 1, 1),
        (date(2026, 1, 31), "month", 1, 2),
        (date(2024, 2, 29), "year", 1, 4),
        (date(2000, 2, 29), "year", 1, 100),
        (date(2026, 12, 31), "day", 2, 2),
        (date(2026, 12, 31), "week", 2, 2),
    ],
)
def test_sql_calendar_matches_fictional_calendar_contract(
    drafts, anchor, frequency, interval, index
):
    with drafts["engine"].connect() as c:
        value = c.scalar(
            text("SELECT coinpup_recurring_date(:a, :f, :gap, :n)"),
            dict(a=anchor, f=frequency, gap=interval, n=index),
        )
        assert value == CalendarSchedule(anchor, frequency, interval).occurrence(index)
        assert (
            c.scalar(
                text("SELECT coinpup_recurring_date(:a, :f, 1, 1)"), dict(a=date.max, f=frequency)
            )
            is None
        )


def test_protected_downgrade_and_empty_round_trip(recurrence):
    s = recurrence
    migration = recurring_migration()
    with s["engine"].begin() as c, Operations.context(MigrationContext.configure(c)):
        migration["downgrade"]()
        migration["upgrade"]()
    create_rule(s)
    before = snapshot(s)
    with pytest.raises(IntegrityError) as failure:
        with s["engine"].begin() as c, Operations.context(MigrationContext.configure(c)):
            migration["downgrade"]()
    assert failure.value.orig.diag.constraint_name == "ck_recurring_downgrade"
    assert snapshot(s) == before
