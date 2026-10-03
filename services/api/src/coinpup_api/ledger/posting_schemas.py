"""Exact original-currency posting commands and immutable receipts."""

import re
from datetime import date, datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, Field, StrictStr, field_validator, model_serializer, model_validator

from coinpup_api.ledger.money import MAX_AMOUNT_STRING_LENGTH
from coinpup_api.ledger.schemas import AssetId, Command

QuantityText = Annotated[
    StrictStr,
    Field(
        min_length=1, max_length=MAX_AMOUNT_STRING_LENGTH, pattern=r"^-?(0|[1-9][0-9]*)(\.[0-9]+)?$"
    ),
]


class PostingMetadata(Command):
    id: UUID | None = None
    transaction_date: date
    description: Annotated[StrictStr, Field(max_length=2000)] = ""

    @field_validator("transaction_date", "recognition_date", mode="before", check_fields=False)
    @classmethod
    def explicit_calendar_date(cls, value):
        if type(value) is date:
            return value
        if isinstance(value, str) and re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
            return value
        raise ValueError("Use a calendar date in YYYY-MM-DD format")

    @field_validator("description")
    @classmethod
    def no_null_character(cls, value):
        if "\x00" in value:
            raise ValueError("Description contains an invalid null character")
        return value


class PostingFields(PostingMetadata):
    asset_id: AssetId
    amount: QuantityText


class PostingCommand(PostingFields):
    account_id: UUID


class OpeningCreate(PostingCommand):
    pass


class PostingSplit(Command):
    category_id: UUID
    amount: QuantityText


class FeeCreate(Command):
    account_id: UUID
    asset_id: AssetId
    amount: QuantityText
    category_id: UUID


Fees = Annotated[list[FeeCreate], Field(max_length=20)]


class ClassifiedCreate(PostingCommand):
    recognition_date: date
    splits: Annotated[list[PostingSplit], Field(min_length=1, max_length=100)]
    fees: Fees = Field(default_factory=list)

    @field_validator("splits")
    @classmethod
    def unique_categories(cls, value):
        if len({item.category_id for item in value}) != len(value):
            raise ValueError("Combine repeated category amounts into a single split")
        return value


class IncomeCreate(ClassifiedCreate):
    pass


class ExpenseCreate(ClassifiedCreate):
    pass


class TransferCreate(PostingFields):
    source_account_id: UUID
    destination_account_id: UUID
    fees: Fees = Field(default_factory=list)

    @model_validator(mode="after")
    def distinct_accounts(self):
        if self.source_account_id == self.destination_account_id:
            raise ValueError("Transfer accounts must be different")
        return self


class ExchangeCreate(PostingMetadata):
    source_account_id: UUID
    source_asset_id: AssetId
    source_amount: QuantityText
    destination_account_id: UUID
    destination_asset_id: AssetId
    destination_amount: QuantityText
    fees: Fees = Field(default_factory=list)

    @model_validator(mode="after")
    def distinct_assets(self):
        if self.source_asset_id == self.destination_asset_id:
            raise ValueError("Exchange assets must be different")
        return self


class SplitResponse(BaseModel):
    category_id: UUID
    amount: str


class FeeResponse(BaseModel):
    account_id: UUID
    asset_id: str
    amount: str
    category_id: UUID


class LegacyFeeResponse(BaseModel):
    fees: list[FeeResponse] = Field(default_factory=list)

    @model_serializer(mode="wrap")
    def preserve_legacy_shape(self, handler):
        data = handler(self)
        if not self.fees:
            data.pop("fees", None)
        return data


class OperationResponse(LegacyFeeResponse):
    id: UUID
    ledger_id: UUID
    journal_id: UUID
    kind: Literal["opening", "income", "expense"]
    version: int
    account_id: UUID
    asset_id: str
    amount: str
    transaction_date: date
    recognition_date: date
    description: str
    splits: list[SplitResponse]
    created_at: datetime


class TransferResponse(LegacyFeeResponse):
    id: UUID
    ledger_id: UUID
    journal_id: UUID
    kind: Literal["transfer"]
    version: int
    source_account_id: UUID
    destination_account_id: UUID
    asset_id: str
    amount: str
    transaction_date: date
    recognition_date: date
    description: str
    created_at: datetime


class ExchangeResponse(BaseModel):
    id: UUID
    ledger_id: UUID
    journal_id: UUID
    kind: Literal["exchange"]
    version: int
    source_account_id: UUID
    source_asset_id: str
    source_amount: str
    destination_account_id: UUID
    destination_asset_id: str
    destination_amount: str
    transaction_date: date
    recognition_date: date
    description: str
    created_at: datetime
    fees: list[FeeResponse] = Field(default_factory=list)


FinancialResponse = Annotated[
    OperationResponse | TransferResponse | ExchangeResponse, Field(discriminator="kind")
]


class BalanceResponse(BaseModel):
    account_id: UUID
    asset_id: str
    amount: str
    account_archived: bool
    asset_enabled: bool
    link_enabled: bool
