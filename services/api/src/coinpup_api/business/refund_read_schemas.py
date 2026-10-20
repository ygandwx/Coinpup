"""Historical actual refunds and separate current original-principal capacity."""

from datetime import date, datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field

from coinpup_api.business.refund_schemas import BusinessRefundResponse
from coinpup_api.ledger.posting_schemas import QuantityText


class RefundSummary(BaseModel):
    id: UUID
    ledger_id: UUID
    payment_id: UUID
    party_id: UUID
    payment_direction: Literal["receipt", "payout"]
    account_id: UUID
    asset_id: str
    amount: QuantityText
    payment_asset_id: str
    payment_amount: QuantityText
    transaction_date: date
    reason: str
    created_at: datetime


class RefundDetail(RefundSummary):
    source_version: int = Field(ge=1)
    receipt: BusinessRefundResponse


class RefundCapacity(BaseModel):
    id: UUID
    ledger_id: UUID
    party_id: UUID
    payment_direction: Literal["receipt", "payout"]
    asset_id: str
    transaction_date: date
    expected_version: int = Field(ge=1)
    source_status: str
    amount: QuantityText
    applied_amount: QuantityText
    refunded_amount: QuantityText
    refundable_amount: QuantityText
