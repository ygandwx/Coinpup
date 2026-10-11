"""Original payment/allocation intent and complete immutable membership receipts."""

from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictInt, model_validator

from coinpup_api.ledger.posting_schemas import (
    CalendarDateFilter,
    Fees,
    PostingCommand,
    QuantityText,
)
from coinpup_api.ledger.schemas import Command

PaymentDirection = Literal["receipt", "payout"]


class AllocationLineCreate(Command):
    document_id: UUID
    document_line_id: UUID
    payment_amount: QuantityText
    document_amount: QuantityText


class InitialAllocationCreate(Command):
    id: UUID
    allocation_date: CalendarDateFilter
    lines: list[AllocationLineCreate] = Field(min_length=1, max_length=200)

    @model_validator(mode="after")
    def unique_sources(self):
        if len({line.document_line_id for line in self.lines}) != len(self.lines):
            raise ValueError("Each allocation names a source line only once")
        return self


class BusinessAllocationCreate(InitialAllocationCreate):
    payment_id: UUID


class BusinessPaymentCreate(PostingCommand):
    id: UUID
    direction: PaymentDirection
    party_id: UUID
    fees: Fees = Field(default_factory=list)
    initial_allocation: InitialAllocationCreate | None = None


class AllocationPosting(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    line_no: Annotated[StrictInt, Field(ge=1, le=200)]
    document_id: UUID
    document_line_id: UUID
    operation_id: UUID
    journal_id: UUID


class BusinessAllocationResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    kind: Literal["business_allocation"]
    id: UUID
    ledger_id: UUID
    payment_id: UUID
    postings: tuple[AllocationPosting, ...] = Field(min_length=1, max_length=200)

    @model_validator(mode="after")
    def complete_ordered_membership(self):
        if [item.line_no for item in self.postings] != list(range(1, len(self.postings) + 1)):
            raise ValueError("Allocation receipt positions are contiguous and ordered")
        for field in ("document_line_id", "operation_id", "journal_id"):
            if len({getattr(item, field) for item in self.postings}) != len(self.postings):
                raise ValueError("Allocation receipt source and posting identities are distinct")
        return self


class BusinessPaymentResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    kind: Literal["business_payment"]
    id: UUID
    ledger_id: UUID
    operation_id: UUID
    journal_id: UUID
    initial_allocation: BusinessAllocationResponse | None

    @model_validator(mode="after")
    def initial_membership(self):
        group = self.initial_allocation
        if group is not None and (
            group.payment_id != self.id
            or group.ledger_id != self.ledger_id
            or any(
                item.operation_id == self.operation_id or item.journal_id == self.journal_id
                for item in group.postings
            )
        ):
            raise ValueError("Initial allocation belongs to this payment with separate postings")
        return self
