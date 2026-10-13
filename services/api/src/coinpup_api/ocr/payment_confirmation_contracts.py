"""Internal payment evidence branch; public confirmation contracts are extended separately."""

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictInt, model_validator

from coinpup_api.business.settlement_read_schemas import BusinessPaymentDetail
from coinpup_api.business.settlement_schemas import BusinessPaymentResponse
from coinpup_api.ledger.schemas import Version

from .contracts import InputModel


class LinkPaymentEntry(InputModel):
    kind: Literal["link_payment"]
    payment_id: UUID
    expected_version: Version = Field(le=2147483647)


class PaymentLinkConfirmationResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    intent_id: UUID
    draft_id: UUID
    ledger_id: UUID
    draft_version: StrictInt = Field(ge=2, le=2147483647)
    action: Literal["link_payment"]
    target_ledger_id: UUID
    target_file_id: UUID
    payment: BusinessPaymentResponse
    operation_version: Version = Field(le=2147483647)

    @model_validator(mode="after")
    def target_membership(self):
        if self.payment.ledger_id != self.target_ledger_id:
            raise ValueError("Original payment belongs to the selected evidence target")
        return self


class PaymentEvidenceTarget(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    payment: BusinessPaymentDetail
    operation_version: Version
    operation_status: Literal["active", "cancelled"]
