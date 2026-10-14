"""Explicit return intent and ordered immutable original-allocation membership."""

from typing import Annotated, Literal
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    StrictStr,
    field_validator,
    model_validator,
)

from coinpup_api.ledger.posting_schemas import CalendarDateFilter, QuantityText
from coinpup_api.ledger.schemas import Command, Version


class AllocationReturnLineCreate(Command):
    source_allocation_line_id: UUID
    expected_version: Version
    payment_amount: QuantityText
    document_amount: QuantityText


class BusinessAllocationReturnCreate(Command):
    id: UUID
    payment_id: UUID
    return_date: CalendarDateFilter
    reason: Annotated[StrictStr, Field(min_length=1, max_length=2000)]
    lines: list[AllocationReturnLineCreate] = Field(min_length=1, max_length=200)

    @field_validator("reason")
    @classmethod
    def explicit_reason(cls, value):
        value = value.strip()
        if not value or "\x00" in value:
            raise ValueError("A non-empty reason without null characters is required")
        return value

    @model_validator(mode="after")
    def distinct_original_mappings(self):
        if len({line.source_allocation_line_id for line in self.lines}) != len(self.lines):
            raise ValueError("Each original allocation mapping is named only once")
        return self


class AllocationReturnPosting(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    line_no: Annotated[StrictInt, Field(ge=1, le=200)]
    source_allocation_line_id: UUID
    source_operation_id: UUID
    document_id: UUID
    document_line_id: UUID
    payment_amount: QuantityText
    document_amount: QuantityText
    operation_id: UUID
    journal_id: UUID

    @field_validator("payment_amount", "document_amount")
    @classmethod
    def bounded_positive_receipt_quantity(cls, value):
        integer, _, fraction = value.partition(".")
        if (
            value.startswith("-")
            or not any(character in "123456789" for character in value)
            or len(integer) > 20
            or len(fraction) > 18
        ):
            raise ValueError("Receipt quantities must be positive and fit the storage bounds")
        return value


class BusinessAllocationReturnResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    kind: Literal["business_allocation_return"]
    id: UUID
    ledger_id: UUID
    payment_id: UUID
    return_date: CalendarDateFilter
    postings: tuple[AllocationReturnPosting, ...] = Field(min_length=1, max_length=200)

    @model_validator(mode="after")
    def complete_original_membership(self):
        if [item.line_no for item in self.postings] != list(range(1, len(self.postings) + 1)):
            raise ValueError("Return receipt positions are contiguous and ordered")
        for field in (
            "source_allocation_line_id",
            "source_operation_id",
            "operation_id",
            "journal_id",
        ):
            if len({getattr(item, field) for item in self.postings}) != len(self.postings):
                raise ValueError("Return receipt original mappings and postings are distinct")
        originals = {item.source_operation_id for item in self.postings}
        if originals & {item.operation_id for item in self.postings}:
            raise ValueError("Return operations are separate from the original operations")
        return self
