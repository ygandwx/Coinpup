"""Authenticated views of owner-scoped reminder email evidence."""

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, Field, StrictStr, model_validator

from coinpup_api.ledger.schemas import Command, Version


class ReminderEmailView(BaseModel):
    id: UUID
    ledger_id: UUID
    event_id: UUID
    channel: Literal["email"]
    planned_at: datetime
    version: int
    attempt_no: int
    state: Literal[
        "sending",
        "retry_wait",
        "failed",
        "unknown",
        "accepted",
        "cancelled",
        "verified_received",
        "stopped",
    ]
    recorded_at: datetime
    retry_at: datetime | None
    smtp_code: int | None


class ReminderEmailDecision(Command):
    expected_version: Version
    action: Literal["retry", "verify_received", "stop"]
    reason: Annotated[StrictStr, Field(min_length=1, max_length=2000)] | None = None

    @model_validator(mode="after")
    def verify_reason(self):
        reason = self.reason
        if self.action == "retry":
            if reason is not None:
                raise ValueError("A retry does not resolve an unknown outcome.")
        elif reason is None or not reason.strip() or "\x00" in reason:
            raise ValueError("Give a reason for the resolution.")
        return self
