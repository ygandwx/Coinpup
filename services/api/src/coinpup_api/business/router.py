"""Authenticated master data endpoints with stable identity and version conflicts."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import Engine

from coinpup_api.access import BrowserAccess
from coinpup_api.auth import Identity
from coinpup_api.business.schemas import (
    PartyCreate,
    PartyResponse,
    PartyUpdate,
    ProjectCreate,
    ProjectResponse,
    ProjectUpdate,
)
from coinpup_api.business.service import BusinessService
from coinpup_api.config import Settings
from coinpup_api.ledger.service import LedgerError


def create_business_router(settings: Settings, engine: Engine | None) -> APIRouter:
    router = APIRouter(prefix="/api/v1/ledgers/{ledger_id}", tags=["business master data"])
    service, access = BusinessService(engine), BrowserAccess(settings, engine)
    reader = Annotated[Identity, Depends(access.read)]
    writer = Annotated[Identity, Depends(access.write)]
    page_size = Annotated[int, Query(ge=1, le=200)]
    page_offset = Annotated[int, Query(ge=0, le=100000)]

    def execute(operation, *args, **kwargs):
        try:
            return operation(*args, **kwargs)
        except LedgerError as error:
            raise HTTPException(error.status, {"code": error.code, "message": str(error)}) from None

    @router.get("/business-parties", response_model=list[PartyResponse])
    def list_parties(
        ledger_id: UUID,
        identity: reader,
        include_archived: bool = True,
        limit: page_size = 100,
        offset: page_offset = 0,
    ):
        return execute(
            service.list_parties,
            identity.id,
            ledger_id,
            include_archived=include_archived,
            limit=limit,
            offset=offset,
        )

    @router.get("/business-parties/{party_id}", response_model=PartyResponse)
    def get_party(ledger_id: UUID, party_id: UUID, identity: reader):
        return execute(service.get_party, identity.id, ledger_id, party_id)

    @router.post("/business-parties", response_model=PartyResponse, status_code=201)
    def create_party(ledger_id: UUID, body: PartyCreate, identity: writer):
        return execute(service.create_party, identity.id, ledger_id, body)

    @router.patch("/business-parties/{party_id}", response_model=PartyResponse)
    def update_party(ledger_id: UUID, party_id: UUID, body: PartyUpdate, identity: writer):
        return execute(service.update_party, identity.id, ledger_id, party_id, body)

    @router.get("/business-projects", response_model=list[ProjectResponse])
    def list_projects(
        ledger_id: UUID,
        identity: reader,
        include_archived: bool = True,
        limit: page_size = 100,
        offset: page_offset = 0,
    ):
        return execute(
            service.list_projects,
            identity.id,
            ledger_id,
            include_archived=include_archived,
            limit=limit,
            offset=offset,
        )

    @router.get("/business-projects/{project_id}", response_model=ProjectResponse)
    def get_project(ledger_id: UUID, project_id: UUID, identity: reader):
        return execute(service.get_project, identity.id, ledger_id, project_id)

    @router.post("/business-projects", response_model=ProjectResponse, status_code=201)
    def create_project(ledger_id: UUID, body: ProjectCreate, identity: writer):
        return execute(service.create_project, identity.id, ledger_id, body)

    @router.patch("/business-projects/{project_id}", response_model=ProjectResponse)
    def update_project(ledger_id: UUID, project_id: UUID, body: ProjectUpdate, identity: writer):
        return execute(service.update_project, identity.id, ledger_id, project_id, body)

    return router
