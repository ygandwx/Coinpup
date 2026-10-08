"""Explicit reasoned closing/reopening commands, separate from financial commands."""

from datetime import date, datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr, field_validator

from coinpup_api.ledger.posting_schemas import CalendarDateFilter
from coinpup_api.ledger.schemas import Command


class PeriodChange(Command):
    action: Literal["close", "reopen"]
    closed_through: CalendarDateFilter | None
    expected_version: Annotated[StrictInt, Field(ge=1, le=2147483646)]
    reason: Annotated[StrictStr, Field(min_length=1, max_length=1000)]

    @field_validator("reason")
    @classmethod
    def explicit_reason(cls, value):
        if not value.strip() or "\x00" in value:
            raise ValueError("A non-empty reason without null characters is required")
        return value.strip()


class PeriodChangeResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    ledger_id: UUID
    actor_id: UUID
    version: int
    action: Literal["close", "reopen"]
    reason: str
    previous_closed_through: date | None
    closed_through: date | None
    created_at: datetime


class PeriodState(BaseModel):
    ledger_id: UUID
    version: Annotated[StrictInt, Field(ge=1)]
    closed_through: date | None
    from_date: date | None
    to_date: date | None
    date_basis: Literal["transaction", "recognition"]
    range_status: Literal["open", "closed", "partial"] | None
    generated_at: datetime
    last_reopened: PeriodChangeResponse | None
