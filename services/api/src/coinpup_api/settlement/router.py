"""Authenticated opt-in interledger advances, repayments, and owned reads."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from sqlalchemy.exc import SQLAlchemyError

from coinpup_api.access import BrowserAccess
from coinpup_api.auth import Identity
from coinpup_api.ledger.posting_router import raw_command_body
from coinpup_api.ledger.service import LedgerError
from coinpup_api.settlement.schemas import (
    InterledgerAdvance,
    InterledgerAdvanceCreate,
    InterledgerDetail,
    InterledgerRepayment,
    InterledgerRepaymentCreate,
    InterledgerSettings,
)
from coinpup_api.settlement.service import InterledgerService


def create_interledger_router(settings, engine):
    router = APIRouter(prefix="/api/v1/interledger", tags=["interledger"])
    service, access = InterledgerService(engine), BrowserAccess(settings, engine)
    reader = Annotated[Identity, Depends(access.read)]
    writer = Annotated[Identity, Depends(access.write)]
    original_body = Annotated[bytes, Depends(raw_command_body)]
    command_key = Annotated[
        str,
        Header(alias="Idempotency-Key", min_length=1, max_length=128, pattern=r"^[!-~]{1,128}$"),
    ]
    page_size = Annotated[int, Query(ge=1, le=200)]
    page_offset = Annotated[int, Query(ge=0, le=100000)]

    def execute(operation, *args, **kwargs):
        try:
            return operation(*args, **kwargs)
        except LedgerError as error:
            raise HTTPException(error.status, {"code": error.code, "message": str(error)}) from None
        except SQLAlchemyError:
            raise HTTPException(
                503,
                {"code": "ledger_unavailable", "message": "Interledger storage is unavailable."},
            ) from None

    @router.get("/settings", response_model=InterledgerSettings)
    def get_settings(identity: reader):
        return execute(service.settings, identity.id)

    @router.put("/settings", response_model=InterledgerSettings)
    def set_settings(payload: InterledgerSettings, identity: writer):
        return execute(service.set_enabled, identity.id, payload)

    @router.get("/advances", response_model=list[InterledgerAdvance])
    def list_advances(identity: reader, limit: page_size = 100, offset: page_offset = 0):
        return execute(service.list_advances, identity.id, limit, offset)

    @router.get("/advances/{advance_id}", response_model=InterledgerDetail)
    def advance_detail(advance_id: UUID, identity: reader):
        return execute(service.detail, identity.id, advance_id)

    @router.post("/advances", response_model=InterledgerAdvance, status_code=201)
    def create_advance(
        payload: InterledgerAdvanceCreate,
        identity: writer,
        idempotency_key: command_key,
        raw_body: original_body,
    ):
        return execute(service.advance, identity.id, payload, idempotency_key, raw_body=raw_body)

    @router.post(
        "/advances/{advance_id}/repayments", response_model=InterledgerRepayment, status_code=201
    )
    def create_repayment(
        advance_id: UUID,
        payload: InterledgerRepaymentCreate,
        identity: writer,
        idempotency_key: command_key,
        raw_body: original_body,
    ):
        return execute(
            service.repay, identity.id, advance_id, payload, idempotency_key, raw_body=raw_body
        )

    return router
