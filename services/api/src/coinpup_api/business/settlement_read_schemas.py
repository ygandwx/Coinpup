"""Read-only original-currency settlement facts and current available quantities."""

from datetime import date, datetime
from typing import Annotated
from uuid import UUID

from pydantic import BaseModel, Field

from coinpup_api.business.settlement_schemas import (
    AllocationPosting,
    BusinessAllocationResponse,
    BusinessPaymentResponse,
    PaymentDirection,
)
from coinpup_api.ledger.posting_schemas import FeeResponse, QuantityText


class BusinessPaymentSummary(BaseModel):
    id: UUID
    ledger_id: UUID
    direction: PaymentDirection
    party_id: UUID
    account_id: UUID
    asset_id: str
    amount: QuantityText
    applied_amount: QuantityText
    unapplied_amount: QuantityText
    transaction_date: date
    description: str
    fee_count: int = Field(ge=0, le=20)
    created_at: datetime


class BusinessPaymentDetail(BusinessPaymentSummary):
    fees: tuple[FeeResponse, ...]
    receipt: BusinessPaymentResponse


class BusinessAllocationSummary(BaseModel):
    id: UUID
    ledger_id: UUID
    payment_id: UUID
    asset_id: str
    payment_amount: QuantityText
    allocation_date: date
    line_count: int = Field(ge=1, le=200)
    is_initial: bool
    created_at: datetime


class BusinessAllocationLineDetail(AllocationPosting):
    id: UUID
    payment_amount: QuantityText
    document_asset_id: str
    document_amount: QuantityText
    project_id: UUID | None
    description: str


class BusinessAllocationDetail(BusinessAllocationSummary):
    lines: tuple[BusinessAllocationLineDetail, ...]
    receipt: Annotated[
        BusinessPaymentResponse | BusinessAllocationResponse, Field(discriminator="kind")
    ]


class DocumentSettlementLine(BaseModel):
    id: UUID
    line_no: int
    project_id: UUID | None
    total_amount: QuantityText
    applied_amount: QuantityText
    outstanding_amount: QuantityText


class DocumentSettlementDetail(BaseModel):
    id: UUID
    ledger_id: UUID
    party_id: UUID
    asset_id: str
    total_amount: QuantityText
    applied_amount: QuantityText
    outstanding_amount: QuantityText
    lines: tuple[DocumentSettlementLine, ...]
