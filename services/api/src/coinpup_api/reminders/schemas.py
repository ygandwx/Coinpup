"""Explicit versioned reminder commands; dates are calendar values, not timestamps."""

from datetime import date, datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    StrictStr,
    field_validator,
    model_validator,
)

from coinpup_api.ledger.posting_schemas import CalendarDateFilter
from coinpup_api.ledger.schemas import Command, Name, RecordResponse, Version
from coinpup_api.reminders.evaluation import Formula, Status

Kind = Literal["annual", "tax", "certificate"]
Reason = Annotated[StrictStr, Field(min_length=1, max_length=500)]


class ReminderCommand(Command):
    @field_validator("title", "reason", "manual_reason", check_fields=False)
    @classmethod
    def clean_text(cls, value):
        if value is not None:
            value = value.strip()
            if not value or any(ord(char) < 32 for char in value):
                raise ValueError("Non-empty text without control characters is required")
        return value

    @field_validator("notes", check_fields=False)
    @classmethod
    def clean_notes(cls, value):
        if value is not None and "\x00" in value:
            raise ValueError("Notes cannot contain NUL characters")
        return value


class RuleSelection(Command):
    rule_id: Annotated[StrictStr, Field(min_length=1, max_length=100)]
    rule_version: Annotated[StrictStr, Field(min_length=1, max_length=40)]
    filing_year: Annotated[StrictInt, Field(ge=1, le=9999)] | None = None
    period_end: CalendarDateFilter | None = None
    expiry_date: CalendarDateFilter | None = None
    applicability_confirmed: StrictBool | None = None


class ReminderCreate(ReminderCommand):
    id: UUID
    event_kind: Kind
    title: Name
    notes: Annotated[StrictStr, Field(max_length=2000)] | None = None
    rule: RuleSelection | None = None
    manual_due_date: CalendarDateFilter | None = None
    manual_reason: Reason | None = None

    @model_validator(mode="after")
    def paired_manual_date(self):
        if (self.manual_due_date is None) != (self.manual_reason is None):
            raise ValueError("Manual date and reason must be provided together")
        return self


class ReminderEdit(ReminderCommand):
    expected_version: Version
    title: Name
    notes: Annotated[StrictStr, Field(max_length=2000)] | None = None


class ReminderRecalculate(ReminderCommand):
    expected_version: Version
    rule: RuleSelection


class ReminderManual(ReminderCommand):
    expected_version: Version
    manual_due_date: CalendarDateFilter | None
    reason: Reason


class ReminderTransition(ReminderCommand):
    expected_version: Version
    action: Literal["complete", "reopen", "archive", "restore"]


class ReminderResponse(RecordResponse):
    id: UUID
    ledger_id: UUID
    event_kind: Kind
    title: str
    notes: str | None
    evaluation: dict | None
    evaluation_status: Status
    calculated_date: date | None
    manual_due_date: date | None
    manual_reason: str | None
    effective_date: date | None
    completed: bool
    archived: bool
    last_actor_id: UUID
    last_action: str
    last_reason: str | None


class ReminderRevisionResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    ledger_id: UUID
    event_id: UUID
    version: int
    snapshot: dict
    created_at: datetime


class ReminderRuleResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    version: str
    checked_on: date
    sources: tuple[str, ...]
    formula: Formula
    required: tuple[str, ...]
    country_code: str | None
    company_type: str | None
    region_code: str | None
    offset_days: int
