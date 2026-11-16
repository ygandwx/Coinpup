"""Authenticated return commands and separately scoped historical reads."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from sqlalchemy.exc import SQLAlchemyError

from coinpup_api.access import BrowserAccess
from coinpup_api.auth import Identity
from coinpup_api.business.allocation_return_current_read import (
    AllocationReturnCurrent,
    BusinessAllocationReturnCurrentRead,
)
from coinpup_api.business.allocation_return_read_schemas import (
    AllocationReturnCapacity,
    AllocationReturnDetail,
    AllocationReturnSummary,
)
from coinpup_api.business.allocation_return_reads import BusinessAllocationReturnReads
from coinpup_api.business.allocation_return_revision_history import BusinessAllocationReturnHistory
from coinpup_api.business.allocation_return_revision_history_schemas import (
    AllocationReturnHistoryEntry,
)
from coinpup_api.business.allocation_return_revision_schemas import (
    AllocationReturnCancellationCreate,
    AllocationReturnCorrectionCreate,
    AllocationReturnRevisionResponse,
)
from coinpup_api.business.allocation_return_revisions import BusinessAllocationReturnRevisionService
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
    history = BusinessAllocationReturnHistory(engine)
    current_reads = BusinessAllocationReturnCurrentRead(engine)
    revisions = BusinessAllocationReturnRevisionService(engine)
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

    @router.post(
        "/business-allocation-returns/{return_id}/corrections",
        response_model=AllocationReturnRevisionResponse,
    )
    def correct(
        ledger_id: UUID,
        return_id: UUID,
        payload: AllocationReturnCorrectionCreate,
        identity: writer,
        idempotency_key: command_key,
        raw_body: original_body,
    ):
        """Reverse the old postings and record the complete ordered replacement group."""
        return execute(
            revisions.correct,
            identity.id,
            ledger_id,
            return_id,
            payload,
            idempotency_key,
            raw_body=raw_body,
        )

    @router.post(
        "/business-allocation-returns/{return_id}/cancellations",
        response_model=AllocationReturnRevisionResponse,
    )
    def cancel(
        ledger_id: UUID,
        return_id: UUID,
        payload: AllocationReturnCancellationCreate,
        identity: writer,
        idempotency_key: command_key,
        raw_body: original_body,
    ):
        """Reverse all current members after explicit release of any active refund references."""
        return execute(
            revisions.cancel,
            identity.id,
            ledger_id,
            return_id,
            payload,
            idempotency_key,
            raw_body=raw_body,
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
        "/business-allocation-returns/{return_id}/current", response_model=AllocationReturnCurrent
    )
    def current(ledger_id: UUID, return_id: UUID, identity: reader):
        """Read the current group version and its ordered active members."""
        return execute(current_reads.current, identity.id, ledger_id, return_id)

    @router.get(
        "/business-allocation-returns/{return_id}/history",
        response_model=list[AllocationReturnHistoryEntry],
    )
    def return_history(
        ledger_id: UUID,
        return_id: UUID,
        identity: reader,
        limit: page_size = 100,
        offset: page_offset = 0,
    ):
        """Page ordered metadata from the original return through its latest revision."""
        return execute(
            history.list_history, identity.id, ledger_id, return_id, limit=limit, offset=offset
        )

    @router.get(
        "/business-allocation-returns/{return_id}/revisions/{revision_id}",
        response_model=AllocationReturnRevisionResponse,
    )
    def revision_detail(ledger_id: UUID, return_id: UUID, revision_id: UUID, identity: reader):
        """Read the complete permanent receipt for this group and revision."""
        return execute(history.get_revision, identity.id, ledger_id, return_id, revision_id)

    @router.get(
        "/business-allocations/{allocation_id}/return-capacity",
        response_model=AllocationReturnCapacity,
    )
    def capacity(ledger_id: UUID, allocation_id: UUID, identity: reader):
        """A current snapshot for an explicit new intent; never an original receipt."""
        return execute(reads.capacity, identity.id, ledger_id, allocation_id)

    return router
