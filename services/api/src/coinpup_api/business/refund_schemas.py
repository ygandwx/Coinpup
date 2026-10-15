"""Explicit original principal and actual cash; no implicit credit or exchange quote."""

from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictStr, field_validator

from coinpup_api.ledger.posting_schemas import CalendarDateFilter, Fees, QuantityText
from coinpup_api.ledger.schemas import AssetId, Command, Version


def _receipt_quantity(value):
    integer, _, fraction = value.partition(".")
    if (
        value.startswith("-")
        or not any(character in "123456789" for character in value)
        or len(integer) > 20
        or len(fraction) > 18
    ):
        raise ValueError("Receipt quantities must be positive and fit the storage bounds")
    return value


class BusinessRefundCreate(Command):
    id: UUID
    payment_id: UUID
    expected_version: Version
    account_id: UUID
    asset_id: AssetId
    amount: QuantityText
    payment_amount: QuantityText
    transaction_date: CalendarDateFilter
    reason: Annotated[StrictStr, Field(min_length=1, max_length=2000)]
    allocation_return_ids: list[UUID] = Field(default_factory=list, max_length=200)
    fees: Fees = Field(default_factory=list)

    @field_validator("reason")
    @classmethod
    def explicit_reason(cls, value):
        value = value.strip()
        if not value or "\x00" in value:
            raise ValueError("A non-empty reason without null characters is required")
        return value

    @field_validator("allocation_return_ids")
    @classmethod
    def distinct_returns(cls, value):
        if len(set(value)) != len(value):
            raise ValueError("Each allocation return is referenced only once")
        return value


class RefundFeeReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    account_id: UUID
    asset_id: StrictStr
    amount: QuantityText
    category_id: UUID

    _positive = field_validator("amount")(_receipt_quantity)


class BusinessRefundResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    kind: Literal["business_refund"]
    id: UUID
    ledger_id: UUID
    payment_id: UUID
    operation_id: UUID
    journal_id: UUID
    account_id: UUID
    asset_id: StrictStr
    amount: QuantityText
    payment_asset_id: StrictStr
    payment_amount: QuantityText
    transaction_date: CalendarDateFilter
    allocation_return_ids: tuple[UUID, ...] = Field(max_length=200)
    fees: tuple[RefundFeeReceipt, ...] = Field(max_length=20)

    @field_validator("allocation_return_ids")
    @classmethod
    def distinct_returns(cls, value):
        if len(set(value)) != len(value):
            raise ValueError("Receipt return references are distinct")
        return value

    _positive = field_validator("amount", "payment_amount")(_receipt_quantity)
