"""Historical actual refunds and separate current original-principal capacity."""

from datetime import date, datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from coinpup_api.business.refund_revision_schemas import RefundRevisionFact
from coinpup_api.business.refund_schemas import BusinessRefundResponse
from coinpup_api.ledger.posting_schemas import QuantityText
from coinpup_api.ledger.schemas import AssetId, Version


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


class RefundCurrent(BaseModel):
    """Authorized current cash fact for a new intention, including its operation version."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    refund_id: UUID
    ledger_id: UUID
    payment_id: UUID
    operation_id: UUID
    party_id: UUID
    payment_direction: Literal["receipt", "payout"]
    payment_asset_id: AssetId
    version: Annotated[Version, Field(le=2147483647)]
    status: Literal["active", "cancelled"]
    revision_id: UUID | None
    revision_reason: str | None = Field(min_length=1, max_length=1000)
    journal_id: UUID | None
    fact: RefundRevisionFact | None

    @model_validator(mode="after")
    def consistent_head(self):
        revised = self.version > 1
        active = self.status == "active"
        if (self.revision_id is not None) != revised or (
            self.revision_reason is not None
        ) != revised:
            raise ValueError("A revised refund requires its revision identity and reason")
        if (
            (self.journal_id is not None) != active
            or (self.fact is not None) != active
            or (not active and not revised)
        ):
            raise ValueError("Only active refunds have a current cash fact and journal")
        if self.fact is not None and self.fact.payment_asset_id != self.payment_asset_id:
            raise ValueError("Current principal must use the original payment asset")
        return self
