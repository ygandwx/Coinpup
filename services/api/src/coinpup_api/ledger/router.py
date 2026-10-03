"""Owner-scoped structure endpoints; no financial posting endpoints yet."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import Engine
from sqlalchemy.exc import IntegrityError

from coinpup_api.access import BrowserAccess
from coinpup_api.auth import Identity
from coinpup_api.config import Settings
from coinpup_api.ledger.schemas import (
    AccountCreate,
    AccountResponse,
    AccountUpdate,
    AssetCreate,
    AssetResponse,
    AssetUpdate,
    CategoryCreate,
    CategoryResponse,
    CategoryUpdate,
    EntityCreate,
    EntityResponse,
    EntityUpdate,
    LedgerResponse,
    TemplateResponse,
)
from coinpup_api.ledger.service import LedgerError, LedgerService


def create_ledger_router(settings: Settings, engine: Engine | None) -> APIRouter:
    router = APIRouter(prefix="/api/v1", tags=["ledger structure"])
    service = LedgerService(engine)
    access = BrowserAccess(settings, engine)
    reader = Annotated[Identity, Depends(access.read)]
    writer = Annotated[Identity, Depends(access.write)]
    page_size = Annotated[int, Query(ge=1, le=200)]
    page_offset = Annotated[int, Query(ge=0, le=100000)]

    def execute(operation, *args, **kwargs):
        try:
            return operation(*args, **kwargs)
        except LedgerError as error:
            raise HTTPException(error.status, {"code": error.code, "message": str(error)}) from None
        except IntegrityError:
            # No SQL, identifiers, parameters or driver diagnostics leave the boundary.
            raise HTTPException(
                409, {"code": "conflict", "message": "The record conflicts with existing data."}
            ) from None

    @router.get("/assets", response_model=list[AssetResponse])
    def list_assets(
        identity: reader,
        include_disabled: bool = True,
        limit: page_size = 100,
        offset: page_offset = 0,
    ):
        return execute(
            service.list_assets,
            identity.id,
            include_disabled=include_disabled,
            limit=limit,
            offset=offset,
        )

    @router.post("/assets", response_model=AssetResponse, status_code=201)
    def create_asset(body: AssetCreate, identity: writer):
        return execute(service.create_asset, identity.id, body)

    @router.patch("/assets/{asset_id}", response_model=AssetResponse)
    def update_asset(asset_id: str, body: AssetUpdate, identity: writer):
        return execute(service.update_asset, identity.id, asset_id, body)

    @router.get("/category-templates", response_model=list[TemplateResponse])
    def templates(identity: reader):
        return execute(service.list_templates, identity.id)

    @router.get("/entities", response_model=list[EntityResponse])
    def list_entities(
        identity: reader,
        include_archived: bool = False,
        limit: page_size = 100,
        offset: page_offset = 0,
    ):
        return execute(
            service.list_entities,
            identity.id,
            include_archived=include_archived,
            limit=limit,
            offset=offset,
        )

    @router.post("/entities", response_model=EntityResponse, status_code=201)
    def create_entity(body: EntityCreate, identity: writer):
        return execute(service.create_entity, identity.id, body)

    @router.get("/entities/{entity_id}", response_model=EntityResponse)
    def get_entity(entity_id: UUID, identity: reader):
        return execute(service.get_entity, identity.id, entity_id)

    @router.patch("/entities/{entity_id}", response_model=EntityResponse)
    def update_entity(entity_id: UUID, body: EntityUpdate, identity: writer):
        return execute(service.update_entity, identity.id, entity_id, body)

    @router.get("/ledgers/{ledger_id}", response_model=LedgerResponse)
    def get_ledger(ledger_id: UUID, identity: reader):
        return execute(service.get_ledger, identity.id, ledger_id)

    @router.get("/ledgers/{ledger_id}/accounts", response_model=list[AccountResponse])
    def list_accounts(
        ledger_id: UUID,
        identity: reader,
        include_archived: bool = False,
        limit: page_size = 100,
        offset: page_offset = 0,
    ):
        return execute(
            service.list_accounts,
            identity.id,
            ledger_id,
            include_archived=include_archived,
            limit=limit,
            offset=offset,
        )

    @router.post("/ledgers/{ledger_id}/accounts", response_model=AccountResponse, status_code=201)
    def create_account(ledger_id: UUID, body: AccountCreate, identity: writer):
        return execute(service.create_account, identity.id, ledger_id, body)

    @router.patch("/ledgers/{ledger_id}/accounts/{account_id}", response_model=AccountResponse)
    def update_account(ledger_id: UUID, account_id: UUID, body: AccountUpdate, identity: writer):
        return execute(service.update_account, identity.id, ledger_id, account_id, body)

    @router.get("/ledgers/{ledger_id}/categories", response_model=list[CategoryResponse])
    def list_categories(
        ledger_id: UUID,
        identity: reader,
        include_archived: bool = False,
        limit: page_size = 100,
        offset: page_offset = 0,
    ):
        return execute(
            service.list_categories,
            identity.id,
            ledger_id,
            include_archived=include_archived,
            limit=limit,
            offset=offset,
        )

    @router.post(
        "/ledgers/{ledger_id}/categories", response_model=CategoryResponse, status_code=201
    )
    def create_category(ledger_id: UUID, body: CategoryCreate, identity: writer):
        return execute(service.create_category, identity.id, ledger_id, body)

    @router.patch("/ledgers/{ledger_id}/categories/{category_id}", response_model=CategoryResponse)
    def update_category(ledger_id: UUID, category_id: UUID, body: CategoryUpdate, identity: writer):
        return execute(service.update_category, identity.id, ledger_id, category_id, body)

    return router
