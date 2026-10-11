"""Stable intent and complete immutable membership receipt for one confirmation."""

from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictInt, model_validator

from coinpup_api.business.schemas import BusinessCommand
from coinpup_api.ledger.schemas import Version


class BusinessConfirmationCreate(BusinessCommand):
    id: UUID
    expected_version: Annotated[Version, Field(le=2147483646)]


class ConfirmationPosting(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    line_id: UUID
    line_no: Annotated[StrictInt, Field(ge=1, le=2147483647)]
    operation_id: UUID
    journal_id: UUID


class BusinessConfirmationResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    kind: Literal["business_confirmation"]
    id: UUID
    ledger_id: UUID
    document_id: UUID
    source_version: Annotated[Version, Field(le=2147483646)]
    document_version: Annotated[Version, Field(le=2147483647)]
    postings: tuple[ConfirmationPosting, ...] = Field(min_length=1, max_length=200)

    @model_validator(mode="after")
    def complete_ordered_membership(self):
        if self.document_version != self.source_version + 1:
            raise ValueError("A confirmation advances the reviewed document once")
        positions = [posting.line_no for posting in self.postings]
        if positions != sorted(set(positions)):
            raise ValueError("Receipt membership has unique ascending line positions")
        for field in ("line_id", "operation_id", "journal_id"):
            if len({getattr(posting, field) for posting in self.postings}) != len(self.postings):
                raise ValueError("Each receipt member has distinct source and posting identities")
        return self
