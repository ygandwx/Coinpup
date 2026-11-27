"""Browser-authorized platform transfer commands and original-asset group reads."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query

from coinpup_api.access import BrowserAccess
from coinpup_api.auth import Identity
from coinpup_api.ledger.platform_transfer_schemas import (
    PlatformTransferCancel,
    PlatformTransferCancellation,
    PlatformTransferCreate,
    PlatformTransferDetail,
    PlatformTransferReceipt,
)
from coinpup_api.ledger.platform_transfers import PlatformTransfers
from coinpup_api.ledger.posting_router import raw_command_body
from coinpup_api.ledger.service import LedgerError


def create_platform_transfer_router(settings, engine):
    router = APIRouter(
        prefix="/api/v1/ledgers/{ledger_id}/platform-transfers", tags=["platform transfers"]
    )
    service, access = PlatformTransfers(engine), BrowserAccess(settings, engine)
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

    @router.get("", response_model=list[PlatformTransferDetail])
    def list_platform_transfers(
        ledger_id: UUID, identity: reader, limit: page_size = 100, offset: page_offset = 0
    ):
        return execute(service.list, identity.id, ledger_id, limit, offset)

    @router.get("/{transfer_id}", response_model=PlatformTransferDetail)
    def platform_transfer_detail(ledger_id: UUID, transfer_id: UUID, identity: reader):
        return execute(service.detail, identity.id, ledger_id, transfer_id)

    @router.post("", response_model=PlatformTransferReceipt, status_code=201)
    def create_platform_transfer(
        ledger_id: UUID,
        body: PlatformTransferCreate,
        identity: writer,
        idempotency_key: command_key,
        raw_body: original_body,
    ):
        return execute(
            service.create, identity.id, ledger_id, body, idempotency_key, raw_body=raw_body
        )

    @router.post("/{transfer_id}/cancel", response_model=PlatformTransferCancellation)
    def cancel_platform_transfer(
        ledger_id: UUID,
        transfer_id: UUID,
        body: PlatformTransferCancel,
        identity: writer,
        idempotency_key: command_key,
        raw_body: original_body,
    ):
        return execute(
            service.cancel,
            identity.id,
            ledger_id,
            transfer_id,
            body,
            idempotency_key,
            raw_body=raw_body,
        )

    return router
