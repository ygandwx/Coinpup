"""Atomic settlement commands and owned historical reads for the online web client."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from sqlalchemy.exc import SQLAlchemyError

from coinpup_api.access import BrowserAccess
from coinpup_api.auth import Identity
from coinpup_api.business.settlement_read_schemas import (
    BusinessAllocationDetail,
    BusinessAllocationSummary,
    BusinessPaymentDetail,
    BusinessPaymentSummary,
    DocumentSettlementDetail,
)
from coinpup_api.business.settlement_reads import BusinessSettlementReads
from coinpup_api.business.settlement_schemas import (
    BusinessAllocationCreate,
    BusinessAllocationResponse,
    BusinessPaymentCreate,
    BusinessPaymentResponse,
)
from coinpup_api.business.settlements import BusinessSettlementService
from coinpup_api.ledger.posting_router import raw_command_body
from coinpup_api.ledger.service import LedgerError


def create_business_settlement_router(settings, engine):
    router = APIRouter(prefix="/api/v1/ledgers/{ledger_id}", tags=["business settlements"])
    service, reads = BusinessSettlementService(engine), BusinessSettlementReads(engine)
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

    @router.post("/business-payments", response_model=BusinessPaymentResponse, status_code=201)
    def pay(
        ledger_id: UUID,
        payload: BusinessPaymentCreate,
        identity: writer,
        idempotency_key: command_key,
        raw_body: original_body,
    ):
        """One payment, optional initial application and all fees commit together."""
        return execute(
            service.pay, identity.id, ledger_id, payload, idempotency_key, raw_body=raw_body
        )

    @router.post(
        "/business-allocations", response_model=BusinessAllocationResponse, status_code=201
    )
    def allocate(
        ledger_id: UUID,
        payload: BusinessAllocationCreate,
        identity: writer,
        idempotency_key: command_key,
        raw_body: original_body,
    ):
        """Retry an unknown result with the same original body and permanent command key."""
        return execute(
            service.allocate, identity.id, ledger_id, payload, idempotency_key, raw_body=raw_body
        )

    @router.get("/business-payments", response_model=list[BusinessPaymentSummary])
    def list_payments(
        ledger_id: UUID, identity: reader, limit: page_size = 100, offset: page_offset = 0
    ):
        return execute(reads.list_payments, identity.id, ledger_id, limit=limit, offset=offset)

    @router.get("/business-payments/{payment_id}", response_model=BusinessPaymentDetail)
    def get_payment(ledger_id: UUID, payment_id: UUID, identity: reader):
        return execute(reads.get_payment, identity.id, ledger_id, payment_id)

    @router.get("/business-allocations", response_model=list[BusinessAllocationSummary])
    def list_allocations(
        ledger_id: UUID,
        identity: reader,
        payment_id: UUID | None = None,
        limit: page_size = 100,
        offset: page_offset = 0,
    ):
        return execute(
            reads.list_allocations,
            identity.id,
            ledger_id,
            payment_id=payment_id,
            limit=limit,
            offset=offset,
        )

    @router.get("/business-allocations/{allocation_id}", response_model=BusinessAllocationDetail)
    def get_allocation(ledger_id: UUID, allocation_id: UUID, identity: reader):
        return execute(reads.get_allocation, identity.id, ledger_id, allocation_id)

    @router.get(
        "/business-documents/{document_id}/settlement", response_model=DocumentSettlementDetail
    )
    def document_capacity(ledger_id: UUID, document_id: UUID, identity: reader):
        return execute(reads.document_capacity, identity.id, ledger_id, document_id)

    return router
