"""Confirmation commands and sealed-document reads share the browser auth boundary."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from sqlalchemy.exc import SQLAlchemyError

from coinpup_api.access import BrowserAccess
from coinpup_api.auth import Identity
from coinpup_api.business.confirmation_reads import (
    BusinessConfirmationDetail,
    BusinessConfirmationReads,
    BusinessConfirmedSummary,
)
from coinpup_api.business.confirmation_schemas import (
    BusinessConfirmationCreate,
    BusinessConfirmationResponse,
)
from coinpup_api.business.confirmations import BusinessConfirmationService
from coinpup_api.ledger.posting_router import raw_command_body
from coinpup_api.ledger.service import LedgerError


def create_business_confirmation_router(settings, engine):
    router = APIRouter(prefix="/api/v1/ledgers/{ledger_id}", tags=["business confirmations"])
    service, reads = BusinessConfirmationService(engine), BusinessConfirmationReads(engine)
    access = BrowserAccess(settings, engine)
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
                503, {"code": "business_unavailable", "message": "Business storage is unavailable."}
            ) from None

    @router.post(
        "/business-documents/{document_id}/confirm",
        response_model=BusinessConfirmationResponse,
        status_code=201,
    )
    def confirm(
        ledger_id: UUID,
        document_id: UUID,
        payload: BusinessConfirmationCreate,
        identity: writer,
        idempotency_key: command_key,
        raw_body: original_body,
    ):
        """Retry unknown submissions with the original key, version, identity and JSON bytes."""
        return execute(
            service.confirm,
            identity.id,
            ledger_id,
            document_id,
            payload,
            idempotency_key,
            raw_body=raw_body,
        )

    @router.get(
        "/business-documents/{document_id}/confirmation", response_model=BusinessConfirmationDetail
    )
    def get_confirmation(ledger_id: UUID, document_id: UUID, identity: reader):
        return execute(reads.get_confirmation, identity.id, ledger_id, document_id)

    @router.get("/business-confirmations", response_model=list[BusinessConfirmedSummary])
    def list_confirmations(
        ledger_id: UUID, identity: reader, limit: page_size = 100, offset: page_offset = 0
    ):
        return execute(reads.list_confirmations, identity.id, ledger_id, limit=limit, offset=offset)

    return router
