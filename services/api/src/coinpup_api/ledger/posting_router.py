"""Authenticated atomic financial commands and original-asset balance reads."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from sqlalchemy import Engine
from sqlalchemy.exc import IntegrityError

from coinpup_api.access import BrowserAccess
from coinpup_api.auth import Identity
from coinpup_api.config import Settings
from coinpup_api.ledger.posting import PostingService
from coinpup_api.ledger.posting_schemas import (
    BalanceResponse,
    ExpenseCreate,
    FinancialResponse,
    IncomeCreate,
    OpeningCreate,
    OperationResponse,
    TransferCreate,
    TransferResponse,
)
from coinpup_api.ledger.service import LedgerError


def create_posting_router(settings: Settings, engine: Engine | None) -> APIRouter:
    router = APIRouter(prefix="/api/v1/ledgers/{ledger_id}", tags=["financial operations"])
    service = PostingService(engine)
    access = BrowserAccess(settings, engine)
    reader = Annotated[Identity, Depends(access.read)]
    writer = Annotated[Identity, Depends(access.write)]
    command_key = Annotated[
        str,
        Header(
            alias="Idempotency-Key",
            min_length=1,
            max_length=128,
            pattern=r"^[!-~]{1,128}$",
            description=(
                "Unique command key within this ledger. Retry with the same validated body. "
                "The original receipt is replayed; a changed body returns 409. "
                "Amount spellings such as 1.0 and 1.00 are different request bodies."
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
        ledger_id: UUID, body: OpeningCreate, identity: writer, idempotency_key: command_key
    ):
        """Set one initial position per account/asset without treating it as income."""
        return execute(service.post_opening, identity.id, ledger_id, body, idempotency_key)

    @router.post("/income", response_model=OperationResponse, status_code=201)
    def income(ledger_id: UUID, body: IncomeCreate, identity: writer, idempotency_key: command_key):
        """Post a receipt and its income-category splits in one transaction."""
        return execute(service.post_income, identity.id, ledger_id, body, idempotency_key)

    @router.post("/expenses", response_model=OperationResponse, status_code=201)
    def expense(
        ledger_id: UUID, body: ExpenseCreate, identity: writer, idempotency_key: command_key
    ):
        """Post a payment and its expense-category splits in one transaction."""
        return execute(service.post_expense, identity.id, ledger_id, body, idempotency_key)

    @router.post("/transfers", response_model=TransferResponse, status_code=201)
    def transfer(
        ledger_id: UUID, body: TransferCreate, identity: writer, idempotency_key: command_key
    ):
        """Move one asset between two accounts, including credit-card repayments."""
        return execute(service.post_transfer, identity.id, ledger_id, body, idempotency_key)

    @router.get("/operations", response_model=list[FinancialResponse])
    def list_operations(
        ledger_id: UUID, identity: reader, limit: page_size = 100, offset: page_offset = 0
    ):
        return execute(service.list_operations, identity.id, ledger_id, limit=limit, offset=offset)

    @router.get("/operations/{operation_id}", response_model=FinancialResponse)
    def get_operation(ledger_id: UUID, operation_id: UUID, identity: reader):
        return execute(service.get_operation, identity.id, ledger_id, operation_id)

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
