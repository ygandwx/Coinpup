"""A complete current group is a new-intent snapshot, never a permanent receipt."""

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from coinpup_api.business.credit_revision_schemas import FactReason, Position, StoredVersion
from coinpup_api.ledger.posting_schemas import CalendarDateFilter, QuantityText
from coinpup_api.ledger.schemas import AssetId


class CreditCurrentMember(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    line_no: Position
    source_line_id: UUID
    source_operation_id: UUID
    operation_id: UUID
    operation_version: StoredVersion
    journal_id: UUID
    net_amount: QuantityText
    tax_amount: QuantityText
    total_amount: QuantityText
    recognition_date: CalendarDateFilter


class CreditCurrent(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    credit_id: UUID
    ledger_id: UUID
    document_id: UUID
    document_kind: Literal["invoice", "bill"]
    party_id: UUID
    asset_id: AssetId
    version: StoredVersion
    status: Literal["active", "cancelled"]
    revision_id: UUID | None
    revision_reason: str | None = Field(max_length=1000)
    credit_date: CalendarDateFilter | None
    credit_reason: FactReason | None
    members: list[CreditCurrentMember] = Field(max_length=200)

    @model_validator(mode="after")
    def complete_current_group(self):
        revised = self.version > 1
        if (self.revision_id is not None) != revised or (
            self.revision_reason is not None
        ) != revised:
            raise ValueError("Only revised groups carry a revision identity and reason")
        if revised and not self.revision_reason.strip():
            raise ValueError("A revision reason must be non-empty")
        active = self.status == "active"
        if (self.credit_date is not None) != active or (self.credit_reason is not None) != active:
            raise ValueError("Only active groups carry a current replacement fact")
        if bool(self.members) != active or (not active and not revised):
            raise ValueError("Cancelled groups have no current members")
        if [member.line_no for member in self.members] != list(range(1, len(self.members) + 1)):
            raise ValueError("Current positions must be complete and ordered")
        for field in ("source_line_id", "source_operation_id", "operation_id", "journal_id"):
            if len({getattr(member, field) for member in self.members}) != len(self.members):
                raise ValueError("Each current source and posting identity is unique")
        return self
