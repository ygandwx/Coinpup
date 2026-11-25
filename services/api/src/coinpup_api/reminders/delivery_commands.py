"""Create or change one reviewed reminder configuration; never send or claim a delivery."""

from typing import Annotated, Literal

from pydantic import Field, StrictBool, StrictInt, StrictStr, field_validator, model_validator
from sqlalchemy import select

from coinpup_api.ledger.schemas import Command, Version
from coinpup_api.ledger.service import LedgerError, _touch, _version
from coinpup_api.reminders.delivery_clock import delivery_zone
from coinpup_api.reminders.delivery_models import ReminderDeliveryConfig as Config
from coinpup_api.reminders.delivery_plan import project_delivery_plan
from coinpup_api.reminders.delivery_reads import delivery_view
from coinpup_api.reminders.mail_attempt import single_mailbox
from coinpup_api.reminders.service import ReminderService


class ConfigureDelivery(Command):
    expected_event_version: Version
    # Zero explicitly witnesses the absence of a configuration.
    expected_config_version: Annotated[StrictInt, Field(ge=0)]
    enabled: StrictBool
    lead_days: Annotated[StrictInt, Field(ge=0, le=3660)]
    timezone: Annotated[StrictStr, Field(min_length=1, max_length=64)]
    minute: Annotated[StrictInt, Field(ge=0, le=1439)]
    locale: Literal["zh", "en"]
    inbox_enabled: StrictBool
    email_enabled: StrictBool
    email_recipient: Annotated[StrictStr, Field(min_length=1, max_length=320)] | None

    @field_validator("timezone")
    @classmethod
    def known_zone(cls, value):
        delivery_zone(value)
        return value

    @field_validator("email_recipient")
    @classmethod
    def explicit_address(cls, value):
        return None if value is None else single_mailbox(value)

    @model_validator(mode="after")
    def validate_channels(self):
        if self.enabled and not (self.email_enabled or self.inbox_enabled):
            raise ValueError("An enabled configuration needs an explicit channel.")
        if self.email_enabled and self.email_recipient is None:
            raise ValueError("The email channel requires one explicit recipient.")
        return self


def store_clock(row, decision):
    row.blocked_reason = decision.blocked_reason
    if decision.plan is not None:
        p = decision.plan
        row.planned_for_date = p.inputs.due
        row.planned_lead_days = p.inputs.lead_days
        row.planned_timezone = p.inputs.zone
        row.planned_minute = p.inputs.minute
        row.planned_at = p.scheduled_at
        row.resolved_minute = p.resolved_minute
        row.planned_tzdata_version = p.tzdata_version


class ReminderDeliveryCommands(ReminderService):
    def configure(self, owner, ledger, identifier, payload: ConfigureDelivery):
        with self._transaction(owner, write=True) as session:
            _, entity = self._ledger(session, owner, ledger, write=True)
            event = self._event(session, ledger, identifier)
            _version(event, payload.expected_event_version)
            if event.archived:
                raise LedgerError("reminder_archived", 409, "Restore the reminder before editing.")
            row = session.scalar(
                select(Config).where(Config.event_id == identifier, Config.ledger_id == ledger)
            )
            if (0 if row is None else row.version) != payload.expected_config_version:
                raise LedgerError("version_conflict", 409, "Reload the delivery configuration.")
            fields = payload.model_dump(
                exclude={"expected_event_version", "expected_config_version"}
            )
            if row is None:
                row = Config(
                    ledger_id=ledger,
                    event_id=identifier,
                    last_actor_id=owner,
                    last_action="create",
                    **fields,
                )
                store_clock(row, project_delivery_plan(row, event, entity_archived=entity.archived))
                session.add(row)
                session.flush()
            else:
                if any(getattr(row, k) != v for k, v in fields.items()):
                    for k, v in fields.items():
                        setattr(row, k, v)
                    # Configure cannot rewrite a clock. If the inputs changed, the next
                    # revision records the new plan within this same transaction.
                    decision = project_delivery_plan(row, event, entity_archived=entity.archived)
                    row.blocked_reason = decision.blocked_reason or (
                        "unplanned" if decision.requires_update else None
                    )
                    row.last_actor_id = owner
                    row.last_action = "configure"
                    _touch(row)
                    session.flush()
                decision = project_delivery_plan(row, event, entity_archived=entity.archived)
                if decision.requires_update:
                    store_clock(row, decision)
                    row.last_actor_id = owner
                    row.last_action = "plan"
                    _touch(row)
                    session.flush()
            return delivery_view(ledger, event, row, entity_archived=entity.archived)
