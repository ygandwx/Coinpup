"""Authenticated return commands and separately scoped historical reads."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from sqlalchemy.exc import SQLAlchemyError

from coinpup_api.access import BrowserAccess
from coinpup_api.auth import Identity
from coinpup_api.business.allocation_return_read_schemas import (
    AllocationReturnCapacity,
    AllocationReturnDetail,
    AllocationReturnSummary,
)
from coinpup_api.business.allocation_return_reads import BusinessAllocationReturnReads
from coinpup_api.business.allocation_return_schemas import (
    BusinessAllocationReturnCreate,
    BusinessAllocationReturnResponse,
)
from coinpup_api.business.allocation_returns import BusinessAllocationReturnService
from coinpup_api.ledger.posting_router import raw_command_body
from coinpup_api.ledger.service import LedgerError


def create_allocation_return_router(settings, engine):
    router = APIRouter(prefix="/api/v1/ledgers/{ledger_id}", tags=["business settlements"])
    service, reads = BusinessAllocationReturnService(engine), BusinessAllocationReturnReads(engine)
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
        "/business-allocation-returns",
        response_model=BusinessAllocationReturnResponse,
        status_code=201,
    )
    def create(
        ledger_id: UUID,
        payload: BusinessAllocationReturnCreate,
        identity: writer,
        idempotency_key: command_key,
        raw_body: original_body,
    ):
        """Release an explicit original application without moving funds or rewriting history."""
        return execute(
            service.create, identity.id, ledger_id, payload, idempotency_key, raw_body=raw_body
        )

    @router.get("/business-allocation-returns", response_model=list[AllocationReturnSummary])
    def list_returns(
        ledger_id: UUID,
        identity: reader,
        payment_id: UUID | None = None,
        limit: page_size = 100,
        offset: page_offset = 0,
    ):
        return execute(
            reads.list_returns,
            identity.id,
            ledger_id,
            payment_id=payment_id,
            limit=limit,
            offset=offset,
        )

    @router.get("/business-allocation-returns/{return_id}", response_model=AllocationReturnDetail)
    def detail(ledger_id: UUID, return_id: UUID, identity: reader):
        return execute(reads.get_return, identity.id, ledger_id, return_id)

    @router.get(
        "/business-allocations/{allocation_id}/return-capacity",
        response_model=AllocationReturnCapacity,
    )
    def capacity(ledger_id: UUID, allocation_id: UUID, identity: reader):
        """A current snapshot for an explicit new intent; never an original receipt."""
        return execute(reads.capacity, identity.id, ledger_id, allocation_id)

    return router
