"""Separate current return capacity from immutable original settlement responses."""

from datetime import date, datetime
from uuid import UUID

from pydantic import BaseModel, Field

from coinpup_api.business.allocation_return_schemas import (
    AllocationReturnPosting,
    BusinessAllocationReturnResponse,
)
from coinpup_api.ledger.posting_schemas import QuantityText


class AllocationReturnCapacityLine(BaseModel):
    source_allocation_line_id: UUID
    source_operation_id: UUID
    expected_version: int = Field(ge=1)
    source_status: str
    document_id: UUID
    document_line_id: UUID
    document_asset_id: str
    description: str
    payment_amount: QuantityText
    document_amount: QuantityText
    returned_payment_amount: QuantityText
    returned_document_amount: QuantityText
    remaining_payment_amount: QuantityText
    remaining_document_amount: QuantityText


class AllocationReturnCapacity(BaseModel):
    id: UUID
    ledger_id: UUID
    payment_id: UUID
    asset_id: str
    allocation_date: date
    lines: tuple[AllocationReturnCapacityLine, ...] = Field(min_length=1, max_length=200)


class AllocationReturnSummary(BaseModel):
    id: UUID
    ledger_id: UUID
    payment_id: UUID
    asset_id: str
    payment_amount: QuantityText
    return_date: date
    reason: str
    line_count: int = Field(ge=1, le=200)
    created_at: datetime


class AllocationReturnLineDetail(AllocationReturnPosting):
    source_allocation_id: UUID
    document_asset_id: str
    description: str


class AllocationReturnDetail(AllocationReturnSummary):
    lines: tuple[AllocationReturnLineDetail, ...] = Field(min_length=1, max_length=200)
    receipt: BusinessAllocationReturnResponse
