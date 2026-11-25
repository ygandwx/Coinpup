"""A read-only plan decision for existing ADR 0038 configuration and event facts."""

from dataclasses import dataclass
from datetime import date, datetime

from coinpup_api.reminders.delivery_clock import DeliveryClockError, schedule_delivery
from coinpup_api.reminders.delivery_models import ReminderDeliveryConfig
from coinpup_api.reminders.models import ReminderEvent


@dataclass(frozen=True)
class ClockInputs:
    due: date
    lead_days: int
    zone: str
    minute: int


@dataclass(frozen=True)
class SavedPlan:
    inputs: ClockInputs
    scheduled_at: datetime
    resolved_minute: int
    tzdata_version: str

    @classmethod
    def from_config(cls, config: ReminderDeliveryConfig):
        if config.planned_at is None:
            return None
        return cls(
            ClockInputs(
                config.planned_for_date,
                config.planned_lead_days,
                config.planned_timezone,
                config.planned_minute,
            ),
            config.planned_at,
            config.resolved_minute,
            config.planned_tzdata_version,
        )


@dataclass(frozen=True)
class PlanDecision:
    plan: SavedPlan | None
    blocked_reason: str | None
    requires_update: bool

    def due(self, now: datetime) -> bool:
        if now.utcoffset() is None:
            raise ValueError("Expected an explicit instant.")
        return (
            self.blocked_reason is None and self.plan is not None and now >= self.plan.scheduled_at
        )


def project_delivery_plan(
    config: ReminderDeliveryConfig, event: ReminderEvent, *, entity_archived: bool
) -> PlanDecision:
    """No clock reads or writes; keep a prior clock witness when an event becomes ineligible."""
    if config.event_id != event.id or config.ledger_id != event.ledger_id:
        raise ValueError("Reminder event and delivery configuration must share scope.")
    saved = SavedPlan.from_config(config)
    due = event.manual_due_date
    if due is None and event.evaluation_status == "calculated":
        due = event.calculated_date
    blocked = next(
        (
            reason
            for condition, reason in [
                (not config.enabled, "disabled"),
                (entity_archived, "archived_entity"),
                (event.archived, "archived_event"),
                (event.completed, "completed"),
                (due is None, "missing_date"),
            ]
            if condition
        ),
        None,
    )
    plan = saved
    if blocked is None:
        inputs = ClockInputs(due, config.lead_days, config.timezone, config.minute)
        if saved is None or saved.inputs != inputs:
            try:
                clock = schedule_delivery(due, config.lead_days, config.timezone, config.minute)
            except DeliveryClockError as exc:
                reasons = {
                    "reminder_delivery_timezone": "timezone_unavailable",
                    "reminder_delivery_date_range": "date_out_of_range",
                    "reminder_delivery_local_date_unavailable": "local_date_unavailable",
                }
                if exc.code not in reasons:
                    raise
                blocked = reasons[exc.code]
            else:
                plan = SavedPlan(
                    inputs, clock.scheduled_at, clock.resolved_minute, clock.tzdata_version
                )
    return PlanDecision(plan, blocked, plan != saved or blocked != config.blocked_reason)
