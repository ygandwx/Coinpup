"""Owned draft inputs echo exact source strings; calculated amounts are server outputs."""

from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import Field, StrictStr, field_validator, model_validator

from coinpup_api.business.pricing import _ratio
from coinpup_api.business.schemas import BusinessCommand, Notes
from coinpup_api.ledger.posting_schemas import CalendarDateFilter
from coinpup_api.ledger.schemas import AssetId, CategoryKind, RecordResponse

RatioText = Annotated[StrictStr, Field(min_length=1, max_length=39)]
PriceText = Annotated[StrictStr, Field(min_length=1, max_length=40)]
Description = Annotated[StrictStr, Field(min_length=1, max_length=2000)]


class DraftLineInput(BusinessCommand):
    id: UUID
    description: Description
    quantity: RatioText
    unit_price: PriceText
    discount_amount: PriceText = "0"
    tax_rate_percent: RatioText = "0"
    category_id: UUID
    project_id: UUID | None = None
    recognition_date: CalendarDateFilter

    @field_validator("description")
    @classmethod
    def clean_description(cls, value):
        value = value.strip()
        if not value or "\x00" in value:
            raise ValueError("A non-empty description without NUL is required")
        return value

    @field_validator("quantity", "tax_rate_percent")
    @classmethod
    def ratio_source(cls, value, info):
        _ratio(value, "pricing_input", positive=info.field_name == "quantity")
        return value

    @field_validator("unit_price", "discount_amount")
    @classmethod
    def money_source(cls, value):
        numerator, _ = _ratio(value.removeprefix("-"), "pricing_input")
        if value.startswith("-") and numerator:
            raise ValueError("Price and discount must be non-negative")
        return value


class DraftHeaderInput(BusinessCommand):
    document_kind: Literal["invoice", "bill"]
    party_id: UUID
    asset_id: AssetId
    issue_date: CalendarDateFilter
    due_date: CalendarDateFilter | None = None
    notes: Notes | None = None


class DraftInput(DraftHeaderInput):
    lines: list[DraftLineInput] = Field(default_factory=list, max_length=200)

    @model_validator(mode="after")
    def distinct_lines(self):
        identifiers = [line.id for line in self.lines]
        if len(set(identifiers)) != len(identifiers):
            raise ValueError("Line identities must be unique")
        return self


class DraftCreate(DraftInput):
    id: UUID


class DraftLineResponse(DraftLineInput, RecordResponse):
    document_id: UUID
    ledger_id: UUID
    line_no: int
    asset_id: str
    category_kind: CategoryKind
    archived: bool
    category_snapshot: dict[str, Any]
    project_snapshot: dict[str, Any] | None
    net_amount: str
    tax_amount: str
    total_amount: str


class DraftSummary(DraftHeaderInput, RecordResponse):
    id: UUID
    ledger_id: UUID
    state: Literal["draft"]
    archived: bool
    issuer_snapshot: dict[str, Any]
    party_snapshot: dict[str, Any]
    line_count: int
    net_amount: str
    tax_amount: str
    total_amount: str


class DraftResponse(DraftSummary):
    lines: list[DraftLineResponse]
