"""Authenticated manual quote management and original report reads."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query

from coinpup_api.access import BrowserAccess
from coinpup_api.auth import Identity
from coinpup_api.ledger.service import LedgerError
from coinpup_api.quotes.schemas import (
    DisplayAsset,
    ManualPricePage,
    ManualPricePut,
    ManualPriceResponse,
    PriceDay,
)
from coinpup_api.quotes.service import ManualPriceService
from coinpup_api.reporting.schemas import ReportResponse, ReportView
from coinpup_api.reporting.service import MvpReportService


def create_reporting_router(settings, engine):
    router = APIRouter(prefix="/api/v1", tags=["reporting"])
    prices, reports = ManualPriceService(engine), MvpReportService(engine)
    access = BrowserAccess(settings, engine)
    reader = Annotated[Identity, Depends(access.read)]
    writer = Annotated[Identity, Depends(access.write)]
    page_size = Annotated[int, Query(ge=1, le=200)]
    page_offset = Annotated[int, Query(ge=0, le=100000)]

    def execute(operation, *args, **kwargs):
        try:
            return operation(*args, **kwargs)
        except LedgerError as error:
            raise HTTPException(error.status, {"code": error.code, "message": str(error)}) from None

    @router.get("/quotes/manual", response_model=ManualPricePage)
    def list_manual_prices(
        identity: reader,
        price_date: PriceDay,
        display_asset_id: DisplayAsset,
        limit: page_size = 100,
        offset: page_offset = 0,
        asset_id: Annotated[str | None, Query(min_length=1, max_length=200)] = None,
    ):
        return execute(
            prices.page,
            identity.id,
            price_date,
            display_asset_id,
            asset_id=asset_id,
            limit=limit,
            offset=offset,
        )

    @router.put("/quotes/manual", response_model=ManualPriceResponse)
    def put_manual_price(payload: ManualPricePut, identity: writer):
        return execute(prices.put, identity.id, payload)

    @router.get("/reports/mvp", response_model=ReportResponse)
    def read_mvp_report(
        identity: reader,
        ledger_ids: Annotated[list[UUID], Query(min_length=1, max_length=100)],
        view: ReportView,
        from_date: PriceDay | None = None,
        to_date: PriceDay | None = None,
        display_asset_id: DisplayAsset | None = None,
        price_date: PriceDay | None = None,
        limit: page_size = 100,
        offset: page_offset = 0,
    ):
        return execute(
            reports.read,
            identity.id,
            ledger_ids,
            view,
            from_date=from_date,
            to_date=to_date,
            display_asset_id=display_asset_id,
            price_date=price_date,
            limit=limit,
            offset=offset,
        )

    return router
