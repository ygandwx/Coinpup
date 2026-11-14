"""Authenticated actual cash refunds and separate original-principal capacity."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from sqlalchemy.exc import SQLAlchemyError

from coinpup_api.access import BrowserAccess
from coinpup_api.auth import Identity
from coinpup_api.business.refund_read_schemas import (
    RefundCapacity,
    RefundCurrent,
    RefundDetail,
    RefundSummary,
)
from coinpup_api.business.refund_reads import BusinessRefundReads
from coinpup_api.business.refund_revision_schemas import (
    RefundCancellationCreate,
    RefundCorrectionCreate,
    RefundRevisionResponse,
)
from coinpup_api.business.refund_revisions import BusinessRefundRevisionService
from coinpup_api.business.refund_schemas import (
    BusinessRefundCreate,
    BusinessRefundResponse,
)
from coinpup_api.business.refunds import BusinessRefundService
from coinpup_api.ledger.posting_router import raw_command_body
from coinpup_api.ledger.service import LedgerError


def create_refund_router(settings, engine):
    router = APIRouter(prefix="/api/v1/ledgers/{ledger_id}", tags=["business settlements"])
    service, reads = BusinessRefundService(engine), BusinessRefundReads(engine)
    revisions = BusinessRefundRevisionService(engine)
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
        "/business-refunds",
        response_model=BusinessRefundResponse,
        status_code=201,
    )
    def create(
        ledger_id: UUID,
        payload: BusinessRefundCreate,
        identity: writer,
        idempotency_key: command_key,
        raw_body: original_body,
    ):
        """Record actual cash independently; preserve classification and the original payment."""
        return execute(
            service.create, identity.id, ledger_id, payload, idempotency_key, raw_body=raw_body
        )

    @router.post(
        "/business-refunds/{refund_id}/corrections",
        response_model=RefundRevisionResponse,
        status_code=200,
    )
    def correct(
        ledger_id: UUID,
        refund_id: UUID,
        payload: RefundCorrectionCreate,
        identity: writer,
        idempotency_key: command_key,
        raw_body: original_body,
    ):
        """Replace current cash, fees and ordered evidence for the same payment."""
        return execute(
            revisions.correct,
            identity.id,
            ledger_id,
            refund_id,
            payload,
            idempotency_key,
            raw_body=raw_body,
        )

    @router.post(
        "/business-refunds/{refund_id}/cancellations",
        response_model=RefundRevisionResponse,
        status_code=200,
    )
    def cancel(
        ledger_id: UUID,
        refund_id: UUID,
        payload: RefundCancellationCreate,
        identity: writer,
        idempotency_key: command_key,
        raw_body: original_body,
    ):
        """Reverse the current refund and retain every prior receipt."""
        return execute(
            revisions.cancel,
            identity.id,
            ledger_id,
            refund_id,
            payload,
            idempotency_key,
            raw_body=raw_body,
        )

    @router.get("/business-refunds", response_model=list[RefundSummary])
    def list_refunds(
        ledger_id: UUID,
        identity: reader,
        payment_id: UUID | None = None,
        limit: page_size = 100,
        offset: page_offset = 0,
    ):
        return execute(
            reads.list_refunds,
            identity.id,
            ledger_id,
            payment_id=payment_id,
            limit=limit,
            offset=offset,
        )

    @router.get("/business-refunds/{refund_id}", response_model=RefundDetail)
    def detail(ledger_id: UUID, refund_id: UUID, identity: reader):
        return execute(reads.get_refund, identity.id, ledger_id, refund_id)

    @router.get("/business-refunds/{refund_id}/current", response_model=RefundCurrent)
    def current(ledger_id: UUID, refund_id: UUID, identity: reader):
        """Read the current operation version and its effective refund, if any."""
        return execute(reads.current, identity.id, ledger_id, refund_id)

    @router.get(
        "/business-payments/{payment_id}/refund-capacity",
        response_model=RefundCapacity,
    )
    def capacity(ledger_id: UUID, payment_id: UUID, identity: reader):
        """A current snapshot for an explicit new intent; never an original receipt."""
        return execute(reads.capacity, identity.id, ledger_id, payment_id)

    return router
