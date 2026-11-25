"""Original-currency reports and exact manual price provenance."""

from datetime import date
from typing import Literal
from uuid import UUID

from pydantic import BaseModel

from coinpup_api.quotes.schemas import AutomaticPriceResponse, DisplayAsset, ManualPriceResponse

ReportView = Literal["balances", "cash", "operating"]
PriceMode = Literal["latest", "historical"]
ValuationState = Literal["not_requested", "identity", "quoted", "missing", "excluded"]


class ReportRow(BaseModel):
    ledger_id: UUID
    source_id: UUID
    asset_id: str
    amount: str
    date: date | None
    operation_id: UUID | None
    account_id: UUID | None
    category_id: UUID | None
    description: str
    repayment_group_id: UUID | None
    excluded_repayment_group: UUID | None
    valuation_state: ValuationState
    price: ManualPriceResponse | None
    automatic_price: AutomaticPriceResponse | None = None


class ReportOriginal(BaseModel):
    asset_id: str
    amount: str
    inflow: str | None
    outflow: str | None


class ReportValuation(BaseModel):
    display_asset_id: DisplayAsset
    price_date: date | None
    amount: str | None
    missing_asset_ids: tuple[str, ...]
    missing_rows: int


class ReportResponse(BaseModel):
    view: ReportView
    rows: tuple[ReportRow, ...]
    originals: tuple[ReportOriginal, ...]
    valuation: ReportValuation | None
    total_rows: int
    limit: int
    offset: int
