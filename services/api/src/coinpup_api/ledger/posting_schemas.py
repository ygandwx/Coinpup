"""Exact original-currency posting commands and immutable receipts."""

import re
from datetime import date, datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, Field, StrictStr, field_validator, model_validator

from coinpup_api.ledger.money import MAX_AMOUNT_STRING_LENGTH
from coinpup_api.ledger.schemas import AssetId, Command

QuantityText = Annotated[
    StrictStr,
    Field(
        min_length=1, max_length=MAX_AMOUNT_STRING_LENGTH, pattern=r"^-?(0|[1-9][0-9]*)(\.[0-9]+)?$"
    ),
]


class PostingFields(Command):
    id: UUID | None = None
    asset_id: AssetId
    amount: QuantityText
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


class PostingCommand(PostingFields):
    account_id: UUID


class OpeningCreate(PostingCommand):
    pass


class PostingSplit(Command):
    category_id: UUID
    amount: QuantityText


class ClassifiedCreate(PostingCommand):
    recognition_date: date
    splits: Annotated[list[PostingSplit], Field(min_length=1, max_length=100)]

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

    @model_validator(mode="after")
    def distinct_accounts(self):
        if self.source_account_id == self.destination_account_id:
            raise ValueError("Transfer accounts must be different")
        return self


class SplitResponse(BaseModel):
    category_id: UUID
    amount: str


class OperationResponse(BaseModel):
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


class TransferResponse(BaseModel):
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


FinancialResponse = Annotated[OperationResponse | TransferResponse, Field(discriminator="kind")]


class BalanceResponse(BaseModel):
    account_id: UUID
    asset_id: str
    amount: str
    account_archived: bool
    asset_enabled: bool
    link_enabled: bool
