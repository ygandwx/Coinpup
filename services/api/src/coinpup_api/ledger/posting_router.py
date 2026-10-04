"""Authenticated atomic financial commands and original-asset balance reads."""

from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from sqlalchemy import Engine
from sqlalchemy.exc import IntegrityError

from coinpup_api.access import BrowserAccess
from coinpup_api.auth import Identity
from coinpup_api.config import Settings
from coinpup_api.ledger.posting import PostingService
from coinpup_api.ledger.posting_schemas import (
    BalanceResponse,
    CalendarDateFilter,
    CancellationCreate,
    CorrectionCreate,
    ExchangeCreate,
    ExchangeResponse,
    ExpenseCreate,
    HistoryEntry,
    IncomeCreate,
    OpeningCreate,
    OperationResponse,
    OperationState,
    TransferCreate,
    TransferResponse,
)
from coinpup_api.ledger.service import LedgerError


async def raw_command_body(request: Request) -> bytes:
    """Starlette caches the original bytes separately from Pydantic's validated model."""
    return await request.body()


def create_posting_router(settings: Settings, engine: Engine | None) -> APIRouter:
    router = APIRouter(prefix="/api/v1/ledgers/{ledger_id}", tags=["financial operations"])
    service = PostingService(engine)
    access = BrowserAccess(settings, engine)
    reader = Annotated[Identity, Depends(access.read)]
    writer = Annotated[Identity, Depends(access.write)]
    original_body = Annotated[bytes, Depends(raw_command_body)]
    command_key = Annotated[
        str,
        Header(
            alias="Idempotency-Key",
            min_length=1,
            max_length=128,
            pattern=r"^[!-~]{1,128}$",
            description=(
                "Unique command key within this ledger. Retry with the same original JSON body. "
                "The original receipt is replayed; a changed body returns 409. "
                "Amount spellings such as 1.0 and 1.00 are different request bodies. "
                "For new v2 receipts, omission, null and empty arrays are distinct; "
                "legacy v1 receipts retain their original rules."
            ),
        ),
    ]
    page_size = Annotated[int, Query(ge=1, le=200)]
    page_offset = Annotated[int, Query(ge=0, le=100000)]

    def execute(operation, *args, **kwargs):
        try:
            return operation(*args, **kwargs)
        except LedgerError as error:
            raise HTTPException(error.status, {"code": error.code, "message": str(error)}) from None
        except IntegrityError:
            raise HTTPException(
                409, {"code": "conflict", "message": "The record conflicts with existing data."}
            ) from None

    @router.post("/opening-balances", response_model=OperationResponse, status_code=201)
    def opening_balance(
        ledger_id: UUID,
        body: OpeningCreate,
        identity: writer,
        idempotency_key: command_key,
        raw_body: original_body,
    ):
        """Set one initial position per account/asset without treating it as income."""
        return execute(
            service.post_opening, identity.id, ledger_id, body, idempotency_key, raw_body=raw_body
        )

    @router.post("/income", response_model=OperationResponse, status_code=201)
    def income(
        ledger_id: UUID,
        body: IncomeCreate,
        identity: writer,
        idempotency_key: command_key,
        raw_body: original_body,
    ):
        """Post a receipt and its income-category splits in one transaction."""
        return execute(
            service.post_income, identity.id, ledger_id, body, idempotency_key, raw_body=raw_body
        )

    @router.post("/expenses", response_model=OperationResponse, status_code=201)
    def expense(
        ledger_id: UUID,
        body: ExpenseCreate,
        identity: writer,
        idempotency_key: command_key,
        raw_body: original_body,
    ):
        """Post a payment and its expense-category splits in one transaction."""
        return execute(
            service.post_expense, identity.id, ledger_id, body, idempotency_key, raw_body=raw_body
        )

    @router.post("/transfers", response_model=TransferResponse, status_code=201)
    def transfer(
        ledger_id: UUID,
        body: TransferCreate,
        identity: writer,
        idempotency_key: command_key,
        raw_body: original_body,
    ):
        """Move one asset between two accounts, including credit-card repayments."""
        return execute(
            service.post_transfer, identity.id, ledger_id, body, idempotency_key, raw_body=raw_body
        )

    @router.post("/exchanges", response_model=ExchangeResponse, status_code=201)
    def exchange(
        ledger_id: UUID,
        body: ExchangeCreate,
        identity: writer,
        idempotency_key: command_key,
        raw_body: original_body,
    ):
        """Record actual exchanged quantities and explicit fees atomically."""
        return execute(
            service.post_exchange, identity.id, ledger_id, body, idempotency_key, raw_body=raw_body
        )

    @router.post("/operations/{operation_id}/corrections", response_model=OperationState)
    def correct_operation(
        ledger_id: UUID,
        operation_id: UUID,
        body: CorrectionCreate,
        identity: writer,
        idempotency_key: command_key,
        raw_body: original_body,
    ):
        """Reverse the prior posting and replace it atomically at the expected version."""
        return execute(
            service.correct_operation,
            identity.id,
            ledger_id,
            operation_id,
            body,
            idempotency_key,
            raw_body=raw_body,
        )

    @router.post("/operations/{operation_id}/cancellations", response_model=OperationState)
    def cancel_operation(
        ledger_id: UUID,
        operation_id: UUID,
        body: CancellationCreate,
        identity: writer,
        idempotency_key: command_key,
        raw_body: original_body,
    ):
        """Reverse the latest posting and retain a terminal cancellation record."""
        return execute(
            service.cancel_operation,
            identity.id,
            ledger_id,
            operation_id,
            body,
            idempotency_key,
            raw_body=raw_body,
        )

    @router.get("/operations", response_model=list[OperationState])
    def list_operations(
        ledger_id: UUID,
        identity: reader,
        limit: page_size = 100,
        offset: page_offset = 0,
        status: Literal["all", "active", "cancelled"] = "all",
        order: Literal["created_at", "transaction_date"] = "created_at",
        from_date: CalendarDateFilter | None = None,
        to_date: CalendarDateFilter | None = None,
    ):
        return execute(
            service.list_operations,
            identity.id,
            ledger_id,
            limit=limit,
            offset=offset,
            status=status,
            order=order,
            from_date=from_date,
            to_date=to_date,
        )

    @router.get("/operations/{operation_id}", response_model=OperationState)
    def get_operation(ledger_id: UUID, operation_id: UUID, identity: reader):
        return execute(service.get_operation, identity.id, ledger_id, operation_id)

    @router.get("/operations/{operation_id}/history", response_model=list[HistoryEntry])
    def operation_history(
        ledger_id: UUID,
        operation_id: UUID,
        identity: reader,
        limit: page_size = 100,
        offset: page_offset = 0,
    ):
        """Read immutable revisions in version order with exact signed audit lines."""
        return execute(
            service.history, identity.id, ledger_id, operation_id, limit=limit, offset=offset
        )

    @router.get("/balances", response_model=list[BalanceResponse])
    def balances(
        ledger_id: UUID,
        identity: reader,
        account_id: UUID | None = None,
        limit: page_size = 100,
        offset: page_offset = 0,
    ):
        """Read signed original quantities, including archived and disabled positions."""
        return execute(
            service.balances,
            identity.id,
            ledger_id,
            account_id=account_id,
            limit=limit,
            offset=offset,
        )

    return router
