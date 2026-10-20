"""Historical credit receipts and distinct current source-line capacity."""

from datetime import date, datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field

from coinpup_api.business.credit_schemas import BusinessCreditResponse
from coinpup_api.ledger.posting_schemas import QuantityText


class CreditSummary(BaseModel):
    id: UUID
    ledger_id: UUID
    document_id: UUID
    document_kind: Literal["invoice", "bill"]
    party_id: UUID
    asset_id: str
    credit_date: date
    reason: str
    created_at: datetime


class CreditDetail(CreditSummary):
    source_version: int = Field(ge=1)
    receipt: BusinessCreditResponse


class CreditLineCapacity(BaseModel):
    source_line_id: UUID
    category_id: UUID
    project_id: UUID | None
    description: str
    recognition_date: date
    net_amount: QuantityText
    tax_amount: QuantityText
    total_amount: QuantityText
    credited_net_amount: QuantityText
    credited_tax_amount: QuantityText
    applied_amount: QuantityText
    remaining_net_amount: QuantityText
    remaining_tax_amount: QuantityText
    available_gross_amount: QuantityText


class CreditCapacity(BaseModel):
    id: UUID
    ledger_id: UUID
    document_kind: Literal["invoice", "bill"]
    party_id: UUID
    asset_id: str
    issue_date: date
    expected_version: int = Field(ge=1)
    lines: list[CreditLineCapacity] = Field(max_length=200)
