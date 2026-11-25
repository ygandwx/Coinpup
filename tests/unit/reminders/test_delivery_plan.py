from datetime import UTC, date, datetime, timedelta
from uuid import UUID

import pytest
from coinpup_api.reminders import delivery_plan
from coinpup_api.reminders.delivery_models import ReminderDeliveryConfig as Config
from coinpup_api.reminders.delivery_plan import project_delivery_plan
from coinpup_api.reminders.models import ReminderEvent

DAY = date(2026, 1, 31)
AT = datetime(2026, 1, 24, 1, tzinfo=UTC)


def subject():
    ledger, item = UUID(int=3), UUID(int=5)
    c = Config(
        id=UUID(int=1),
        ledger_id=ledger,
        event_id=item,
        enabled=True,
        minute=540,
        lead_days=7,
        timezone="Asia/Shanghai",
        blocked_reason=None,
        planned_for_date=DAY,
        planned_at=AT,
        planned_timezone="Asia/Shanghai",
        planned_minute=540,
        planned_lead_days=7,
        resolved_minute=540,
        planned_tzdata_version="fictional-prior",
    )
    e = ReminderEvent(
        id=item,
        ledger_id=ledger,
        manual_due_date=None,
        evaluation_status="calculated",
        calculated_date=DAY,
        completed=False,
        archived=False,
    )
    return c, e


def test_frozen_time_is_reused_across_label_recipient_or_tzdata_changes(monkeypatch):
    config, event = subject()
    event.title = "Fictional edited title"
    config.email_recipient = "different@example.test"
    monkeypatch.setattr(
        delivery_plan, "schedule_delivery", lambda *a: pytest.fail("Must reuse persisted UTC")
    )
    d = project_delivery_plan(config, event, entity_archived=False)
    assert not d.requires_update and d.plan.tzdata_version == "fictional-prior"
    assert (
        not d.due(AT - timedelta(microseconds=1)) and d.due(AT) and d.due(AT + timedelta(days=30))
    )
    with pytest.raises(ValueError):
        d.due(AT.replace(tzinfo=None))


@pytest.mark.parametrize(
    "attr,value,reason",
    [
        ("enabled", False, "disabled"),
        ("archived", True, "archived_event"),
        ("completed", True, "completed"),
        ("entity_archived", True, "archived_entity"),
        ("evaluation_status", "needs_verification", "missing_date"),
        ("evaluation_status", "not_applicable", "missing_date"),
    ],
)
def test_ineligible_event_cannot_deliver_even_when_the_persisted_clock_is_overdue(
    attr, value, reason
):
    c, e = subject()
    if attr != "entity_archived":
        setattr(c if attr == "enabled" else e, attr, value)
    d = project_delivery_plan(c, e, entity_archived=attr == "entity_archived")
    assert d.blocked_reason == reason and d.requires_update and not d.due(AT + timedelta(days=90))
    assert d.plan.scheduled_at == AT and c.planned_at == AT and c.blocked_reason is None


def test_explicit_manual_date_moves_delivery_earlier_even_if_last_plan_is_in_future():
    c, e = subject()
    e.evaluation_status = "needs_verification"
    e.manual_due_date = DAY - timedelta(days=10)
    d = project_delivery_plan(c, e, entity_archived=False)
    assert (
        d.requires_update
        and d.blocked_reason is None
        and d.plan.scheduled_at == AT - timedelta(days=10)
    )
    assert d.due(AT - timedelta(days=9)) and c.planned_at == AT


@pytest.mark.parametrize(
    "zone,date_value,lead,reason",
    [
        ("Nowhere/Fictional", DAY, 7, "timezone_unavailable"),
        ("UTC", date.min, 7, "date_out_of_range"),
        ("Pacific/Apia", date(2011, 12, 30), 0, "local_date_unavailable"),
    ],
)
def test_unresolvable_changed_clock_blocks_and_never_uses_the_previously_ready_time(
    zone, date_value, lead, reason
):
    c, e = subject()
    c.timezone = zone
    c.lead_days = lead
    e.manual_due_date = date_value
    decision = project_delivery_plan(c, e, entity_archived=False)
    assert (
        decision.blocked_reason == reason
        and decision.plan.scheduled_at == AT
        and not decision.due(AT)
    )


@pytest.mark.parametrize("attribute", ["ledger_id", "id"])
def test_projection_refuses_a_foreign_event_even_if_the_time_parameters_match(attribute):
    c, e = subject()
    setattr(e, attribute, UUID(int=9))
    with pytest.raises(ValueError, match="share scope"):
        project_delivery_plan(c, e, entity_archived=False)
