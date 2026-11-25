"""Owner and ledger scoped read of a captured reminder plan and its current preview."""

from datetime import date, datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict
from sqlalchemy import and_, select

from coinpup_api.ledger.service import LedgerService, _not_found
from coinpup_api.reminders.delivery_models import ReminderDeliveryConfig as Config
from coinpup_api.reminders.delivery_plan import SavedPlan, project_delivery_plan
from coinpup_api.reminders.models import ReminderEvent as Event

Blocked = Literal[
    "disabled",
    "unplanned",
    "missing_date",
    "completed",
    "archived_event",
    "archived_entity",
    "timezone_unavailable",
    "date_out_of_range",
    "local_date_unavailable",
]


class ClockWitness(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    due_date: date
    lead_days: int
    timezone: str
    requested_minute: int
    resolved_minute: int
    scheduled_at: datetime
    tzdata_version: str


class DeliveryConfigView(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    id: UUID
    version: int
    enabled: bool
    lead_days: int
    timezone: str
    minute: int
    locale: Literal["zh", "en"]
    inbox_enabled: bool
    email_enabled: bool
    email_recipient: str | None
    saved_plan: ClockWitness | None
    saved_blocked_reason: Blocked | None
    preview_plan: ClockWitness | None
    preview_blocked_reason: Blocked | None
    needs_refresh: bool


class ReminderDeliveryView(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    ledger_id: UUID
    event_id: UUID
    event_version: int
    config: DeliveryConfigView | None


def _witness(plan: SavedPlan | None):
    if plan is None:
        return None
    return ClockWitness(
        due_date=plan.inputs.due,
        lead_days=plan.inputs.lead_days,
        timezone=plan.inputs.zone,
        requested_minute=plan.inputs.minute,
        resolved_minute=plan.resolved_minute,
        scheduled_at=plan.scheduled_at,
        tzdata_version=plan.tzdata_version,
    )


def delivery_view(ledger, event, config, *, entity_archived):
    current = None
    if config is not None:
        preview = project_delivery_plan(config, event, entity_archived=entity_archived)
        current = DeliveryConfigView(
            id=config.id,
            version=config.version,
            enabled=config.enabled,
            lead_days=config.lead_days,
            timezone=config.timezone,
            minute=config.minute,
            locale=config.locale,
            inbox_enabled=config.inbox_enabled,
            email_enabled=config.email_enabled,
            email_recipient=config.email_recipient,
            saved_plan=_witness(SavedPlan.from_config(config)),
            saved_blocked_reason=config.blocked_reason,
            preview_plan=_witness(preview.plan),
            preview_blocked_reason=preview.blocked_reason,
            needs_refresh=preview.requires_update,
        )
    return ReminderDeliveryView(
        ledger_id=ledger, event_id=event.id, event_version=event.version, config=current
    )


class ReminderDeliveryReads(LedgerService):
    def get(self, owner, ledger, identifier):
        with self._transaction(owner, read_only=True) as session:
            _, entity = self._ledger(session, owner, ledger)
            row = session.execute(
                select(Event, Config)
                .outerjoin(
                    Config, and_(Config.event_id == Event.id, Config.ledger_id == Event.ledger_id)
                )
                .where(Event.ledger_id == ledger, Event.id == identifier)
            ).one_or_none()
            if row is None:
                raise _not_found()
            event, config = row
            return delivery_view(ledger, event, config, entity_archived=entity.archived)
