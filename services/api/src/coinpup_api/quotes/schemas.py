"""Exact quote wire contracts, independent of amounts posted in a ledger."""

from datetime import date, datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import (
    AfterValidator,
    AwareDatetime,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    StrictInt,
    StrictStr,
    model_validator,
)

DisplayAsset = Literal["USD", "GBP", "EUR", "HKD", "CNY"]
QuoteAsset = Annotated[StrictStr, Field(min_length=1, max_length=200)]


def _positive_price(value: str) -> str:
    if not any("1" <= digit <= "9" for digit in value):
        raise ValueError("Price must be positive.")
    return value


def _printable_source(value: str) -> str:
    if value.strip() != value or not value.isprintable():
        raise ValueError("Expected a printable, trimmed price source.")
    return value


def _exact_day(value) -> date:
    if type(value) is date:
        return value
    if type(value) is not str or len(value) != 10:
        raise ValueError("Expected an ISO calendar date.")
    parsed = date.fromisoformat(value)
    if parsed.isoformat() != value:
        raise ValueError("Expected an ISO calendar date.")
    return parsed


PriceDay = Annotated[date, BeforeValidator(_exact_day)]
PriceText = Annotated[
    StrictStr,
    Field(min_length=1, max_length=39, pattern=r"^(0|[1-9][0-9]{0,19})([.][0-9]{1,18})?$"),
    AfterValidator(_positive_price),
]
PriceSource = Annotated[
    StrictStr, Field(min_length=1, max_length=128), AfterValidator(_printable_source)
]


class ManualPricePut(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    asset_id: QuoteAsset
    display_asset_id: DisplayAsset
    price_date: PriceDay
    price: PriceText
    source: PriceSource
    expected_version: Annotated[StrictInt, Field(ge=0, le=2_147_483_647)]

    @model_validator(mode="after")
    def distinct_assets(self):
        if self.asset_id == self.display_asset_id:
            raise ValueError("A display asset needs no self-rate.")
        return self


class ManualPriceResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True, frozen=True)

    id: UUID
    asset_id: QuoteAsset
    display_asset_id: DisplayAsset
    price_date: PriceDay
    price: PriceText
    source: PriceSource
    version: Annotated[StrictInt, Field(ge=1)]
    updated_at: datetime


class ManualPricePage(BaseModel):
    items: tuple[ManualPriceResponse, ...]
    limit: int
    offset: int
    has_more: bool


class AutomaticPriceResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True, frozen=True)

    id: UUID
    asset_id: QuoteAsset
    display_asset_id: DisplayAsset
    price_date: PriceDay
    price: PriceText
    source: Literal["Frankfurter / ECB", "Coinbase Exchange"]
    observed_at: AwareDatetime | None
    fetched_at: AwareDatetime


class EffectivePriceResponse(BaseModel):
    asset_id: QuoteAsset
    state: Literal["identity", "manual", "automatic", "missing"]
    manual_price: ManualPriceResponse | None = None
    automatic_price: AutomaticPriceResponse | None = None


class EffectivePricePage(BaseModel):
    price_date: PriceDay
    display_asset_id: DisplayAsset
    items: tuple[EffectivePriceResponse, ...]
