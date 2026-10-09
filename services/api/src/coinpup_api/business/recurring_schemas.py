"""Versioned recurring draft inputs; changing a calendar requires a replacement rule."""

from typing import Annotated
from uuid import UUID

from pydantic import Field, StrictBool, StrictInt, StrictStr, field_validator, model_validator

from coinpup_api.business.draft_schemas import BusinessDraftCreate
from coinpup_api.business.recurrence_calendar import Frequency
from coinpup_api.business.schemas import BusinessCommand
from coinpup_api.ledger.posting_schemas import CalendarDateFilter
from coinpup_api.ledger.schemas import Name, RecordResponse, Version


class RecurringRuleCreate(BusinessCommand):
    id: UUID
    name: Name
    timezone_name: Annotated[StrictStr, Field(min_length=1, max_length=100)]
    anchor_date: CalendarDateFilter
    frequency: Frequency
    interval_count: Annotated[StrictInt, Field(ge=1, le=120)] = 1
    source_document_id: UUID
    source_version: Version

    @field_validator("timezone_name")
    @classmethod
    def timezone_text(cls, value):
        if value != value.strip() or "\x00" in value:
            raise ValueError("An explicit timezone name without whitespace or NUL is required")
        return value


class RecurringRuleUpdate(BusinessCommand):
    expected_version: Version
    name: Name
    source_document_id: UUID | None = None
    source_version: Version | None = None

    @model_validator(mode="after")
    def complete_source(self):
        if (self.source_document_id is None) != (self.source_version is None):
            raise ValueError("Template refresh requires both source identity and version")
        return self


class RecurringRuleArchive(BusinessCommand):
    expected_version: Version
    archived: StrictBool


class RecurringRuleResponse(RecurringRuleCreate, RecordResponse):
    ledger_id: UUID
    archived: bool
    next_index: int
    template_input: BusinessDraftCreate


class RecurringInstanceResponse(RecordResponse):
    id: UUID
    ledger_id: UUID
    rule_id: UUID
    occurrence_index: int
    scheduled_date: CalendarDateFilter
    rule_version: int
    original_input: BusinessDraftCreate
    archived: bool
