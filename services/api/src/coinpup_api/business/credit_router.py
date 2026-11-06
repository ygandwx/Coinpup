"""Authenticated manual credits with immutable receipts and source capacity."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from sqlalchemy.exc import SQLAlchemyError

from coinpup_api.access import BrowserAccess
from coinpup_api.auth import Identity
from coinpup_api.business.credit_read_schemas import (
    CreditCapacity,
    CreditDetail,
    CreditSummary,
)
from coinpup_api.business.credit_reads import BusinessCreditReads
from coinpup_api.business.credit_revision_schemas import (
    CreditCancellationCreate,
    CreditCorrectionCreate,
    CreditRevisionResponse,
)
from coinpup_api.business.credit_revisions import BusinessCreditRevisionService
from coinpup_api.business.credit_schemas import (
    BusinessCreditCreate,
    BusinessCreditResponse,
)
from coinpup_api.business.credits import BusinessCreditService
from coinpup_api.ledger.posting_router import raw_command_body
from coinpup_api.ledger.service import LedgerError


def create_credit_router(settings, engine):
    router = APIRouter(prefix="/api/v1/ledgers/{ledger_id}", tags=["business settlements"])
    service, reads = BusinessCreditService(engine), BusinessCreditReads(engine)
    revisions = BusinessCreditRevisionService(engine)
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
        "/business-credits",
        response_model=BusinessCreditResponse,
        status_code=201,
    )
    def create(
        ledger_id: UUID,
        payload: BusinessCreditCreate,
        identity: writer,
        idempotency_key: command_key,
        raw_body: original_body,
    ):
        """Reduce the original classification without changing actual cash or old receipts."""
        return execute(
            service.create, identity.id, ledger_id, payload, idempotency_key, raw_body=raw_body
        )

    @router.post(
        "/business-credits/{credit_id}/corrections",
        response_model=CreditRevisionResponse,
        status_code=200,
    )
    def correct(
        ledger_id: UUID,
        credit_id: UUID,
        payload: CreditCorrectionCreate,
        identity: writer,
        idempotency_key: command_key,
        raw_body: original_body,
    ):
        """Replace the complete current group; replay the original key and unchanged body."""
        return execute(
            revisions.correct,
            identity.id,
            ledger_id,
            credit_id,
            payload,
            idempotency_key,
            raw_body=raw_body,
        )

    @router.post(
        "/business-credits/{credit_id}/cancellations",
        response_model=CreditRevisionResponse,
        status_code=200,
    )
    def cancel(
        ledger_id: UUID,
        credit_id: UUID,
        payload: CreditCancellationCreate,
        identity: writer,
        idempotency_key: command_key,
        raw_body: original_body,
    ):
        """Reverse every current member without a refund or alteration of old receipts."""
        return execute(
            revisions.cancel,
            identity.id,
            ledger_id,
            credit_id,
            payload,
            idempotency_key,
            raw_body=raw_body,
        )

    @router.get("/business-credits", response_model=list[CreditSummary])
    def list_credits(
        ledger_id: UUID,
        identity: reader,
        document_id: UUID | None = None,
        limit: page_size = 100,
        offset: page_offset = 0,
    ):
        return execute(
            reads.list_credits,
            identity.id,
            ledger_id,
            document_id=document_id,
            limit=limit,
            offset=offset,
        )

    @router.get("/business-credits/{credit_id}", response_model=CreditDetail)
    def detail(ledger_id: UUID, credit_id: UUID, identity: reader):
        return execute(reads.get_credit, identity.id, ledger_id, credit_id)

    @router.get(
        "/business-documents/{document_id}/credit-capacity",
        response_model=CreditCapacity,
    )
    def capacity(
        ledger_id: UUID,
        document_id: UUID,
        identity: reader,
        limit: page_size = 100,
        offset: page_offset = 0,
    ):
        """A current snapshot for an explicit new intent; never an original receipt."""
        return execute(
            reads.capacity, identity.id, ledger_id, document_id, limit=limit, offset=offset
        )

    return router
