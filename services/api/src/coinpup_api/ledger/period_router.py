"""Owned period status, reasoned commands and bounded immutable audit reads."""

from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query

from coinpup_api.access import BrowserAccess
from coinpup_api.auth import Identity
from coinpup_api.ledger.period_reads import PeriodReads
from coinpup_api.ledger.period_schemas import PeriodChange, PeriodChangeResponse, PeriodState
from coinpup_api.ledger.period_service import PeriodService
from coinpup_api.ledger.posting_router import raw_command_body
from coinpup_api.ledger.posting_schemas import CalendarDateFilter
from coinpup_api.ledger.service import LedgerError


def create_period_router(settings, engine):
    router = APIRouter(prefix="/api/v1/ledgers/{ledger_id}", tags=["ledger periods"])
    reads, commands, access = (
        PeriodReads(engine),
        PeriodService(engine),
        BrowserAccess(settings, engine),
    )
    reader = Annotated[Identity, Depends(access.read)]
    writer = Annotated[Identity, Depends(access.write)]
    original_body = Annotated[bytes, Depends(raw_command_body)]
    key = Annotated[
        str,
        Header(alias="Idempotency-Key", min_length=1, max_length=128, pattern=r"^[!-~]{1,128}$"),
    ]

    def execute(call, *args, **kwargs):
        try:
            return call(*args, **kwargs)
        except LedgerError as error:
            raise HTTPException(error.status, {"code": error.code, "message": str(error)}) from None

    @router.get("/period", response_model=PeriodState)
    def state(
        ledger_id: UUID,
        identity: reader,
        from_date: CalendarDateFilter | None = None,
        to_date: CalendarDateFilter | None = None,
        date_basis: Literal["transaction", "recognition"] = "transaction",
    ):
        return execute(
            reads.state,
            identity.id,
            ledger_id,
            from_date=from_date,
            to_date=to_date,
            date_basis=date_basis,
        )

    @router.get("/period-changes", response_model=list[PeriodChangeResponse])
    def history(
        ledger_id: UUID,
        identity: reader,
        limit: Annotated[int, Query(ge=1, le=200)] = 100,
        offset: Annotated[int, Query(ge=0, le=100000)] = 0,
    ):
        return execute(reads.history, identity.id, ledger_id, limit=limit, offset=offset)

    @router.post("/period-changes", response_model=PeriodChangeResponse, status_code=201)
    def change(
        ledger_id: UUID,
        body: PeriodChange,
        identity: writer,
        idempotency_key: key,
        raw_body: original_body,
    ):
        return execute(
            commands.change, identity.id, ledger_id, body, idempotency_key, raw_body=raw_body
        )

    return router
