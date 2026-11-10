"""Bounded metadata only; complete immutable members belong to a single revision detail."""

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr, model_validator


class CreditHistoryEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    version: Annotated[StrictInt, Field(ge=1, le=2147483647)]
    revision_id: UUID | None
    action: Literal["create", "correct", "cancel"]
    reason: Annotated[StrictStr, Field(min_length=1, max_length=2000)]
    created_at: datetime

    @model_validator(mode="after")
    def original_or_revision(self):
        if (self.action == "create") != (self.version == 1) or (self.revision_id is None) != (
            self.version == 1
        ):
            raise ValueError("Only the original creation has no revision identifier")
        return self
