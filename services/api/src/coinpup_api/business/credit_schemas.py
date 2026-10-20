"""Explicit manual credit intent and complete immutable source-line receipts."""

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
from coinpup_api.ledger.schemas import AssetId, Command, Version


class CreditLineCreate(Command):
    source_line_id: UUID
    net_amount: QuantityText
    tax_amount: QuantityText
    recognition_date: CalendarDateFilter


class BusinessCreditCreate(Command):
    id: UUID
    document_id: UUID
    expected_version: Annotated[Version, Field(le=2147483647)]
    credit_date: CalendarDateFilter
    reason: Annotated[StrictStr, Field(min_length=1, max_length=2000)]
    lines: list[CreditLineCreate] = Field(min_length=1, max_length=200)

    @field_validator("reason")
    @classmethod
    def explicit_reason(cls, value):
        value = value.strip()
        if not value or "\x00" in value:
            raise ValueError("A non-empty reason without null characters is required")
        return value

    @model_validator(mode="after")
    def distinct_sources(self):
        if len({line.source_line_id for line in self.lines}) != len(self.lines):
            raise ValueError("Each original document line is named only once")
        return self


def _receipt_units(value):
    integer, _, fraction = value.partition(".")
    if value.startswith("-") or len(integer) > 20 or len(fraction) > 18:
        raise ValueError("Credit receipt quantities are nonnegative and fit storage bounds")
    return int(integer) * 10**18 + int(fraction.ljust(18, "0"))


class CreditPosting(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    line_no: Annotated[StrictInt, Field(ge=1, le=200)]
    source_line_id: UUID
    source_operation_id: UUID
    net_amount: QuantityText
    tax_amount: QuantityText
    total_amount: QuantityText
    recognition_date: CalendarDateFilter
    operation_id: UUID
    journal_id: UUID

    @model_validator(mode="after")
    def exact_manual_components(self):
        net, tax, total = map(_receipt_units, (self.net_amount, self.tax_amount, self.total_amount))
        if total <= 0 or net + tax != total:
            raise ValueError("Credit receipt net and tax exactly sum to a positive total")
        return self


class BusinessCreditResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    kind: Literal["business_credit"]
    id: UUID
    ledger_id: UUID
    document_id: UUID
    source_version: Annotated[Version, Field(le=2147483647)]
    asset_id: AssetId
    credit_date: CalendarDateFilter
    postings: tuple[CreditPosting, ...] = Field(min_length=1, max_length=200)

    @model_validator(mode="after")
    def complete_original_membership(self):
        if [item.line_no for item in self.postings] != list(range(1, len(self.postings) + 1)):
            raise ValueError("Credit receipt positions are contiguous and ordered")
        for field in ("source_line_id", "source_operation_id", "operation_id", "journal_id"):
            if len({getattr(item, field) for item in self.postings}) != len(self.postings):
                raise ValueError("Credit receipt source and posting identities are distinct")
        if {item.source_operation_id for item in self.postings} & {
            item.operation_id for item in self.postings
        }:
            raise ValueError("Credit operations are separate from original accrual operations")
        return self
