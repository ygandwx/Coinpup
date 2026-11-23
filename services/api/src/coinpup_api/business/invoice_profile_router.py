"""Owned configuration endpoints; no public number allocation or Invoice issuance."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import Engine

from coinpup_api.access import BrowserAccess
from coinpup_api.auth import Identity
from coinpup_api.business.invoice_issuance_schemas import (
    InvoiceProfileArchive,
    InvoiceProfileCreate,
    InvoiceProfileReplacement,
)
from coinpup_api.business.invoice_profiles import InvoiceProfileResponse, InvoiceProfileService
from coinpup_api.config import Settings
from coinpup_api.ledger.service import LedgerError


def create_invoice_profile_router(settings: Settings, engine: Engine | None) -> APIRouter:
    router = APIRouter(
        prefix="/api/v1/ledgers/{ledger_id}/invoice-profiles", tags=["Invoice configuration"]
    )
    service, access = InvoiceProfileService(engine), BrowserAccess(settings, engine)
    reader = Annotated[Identity, Depends(access.read)]
    writer = Annotated[Identity, Depends(access.write)]
    page_size = Annotated[int, Query(ge=1, le=200)]
    page_offset = Annotated[int, Query(ge=0, le=100000)]

    def execute(operation, *args, **kwargs):
        try:
            return operation(*args, **kwargs)
        except LedgerError as error:
            raise HTTPException(error.status, {"code": error.code, "message": str(error)}) from None

    @router.get("", response_model=list[InvoiceProfileResponse])
    def list_profiles(
        ledger_id: UUID,
        identity: reader,
        include_archived: bool = True,
        limit: page_size = 100,
        offset: page_offset = 0,
    ):
        return execute(
            service.list,
            identity.id,
            ledger_id,
            include_archived=include_archived,
            limit=limit,
            offset=offset,
        )

    @router.get("/{profile_id}", response_model=InvoiceProfileResponse)
    def get_profile(ledger_id: UUID, profile_id: UUID, identity: reader):
        return execute(service.get, identity.id, ledger_id, profile_id)

    @router.post("", response_model=InvoiceProfileResponse, status_code=201)
    def create_profile(ledger_id: UUID, body: InvoiceProfileCreate, identity: writer):
        return execute(service.create, identity.id, ledger_id, body)

    @router.put("/{profile_id}", response_model=InvoiceProfileResponse)
    def replace_profile(
        ledger_id: UUID, profile_id: UUID, body: InvoiceProfileReplacement, identity: writer
    ):
        return execute(service.replace, identity.id, ledger_id, profile_id, body)

    @router.patch("/{profile_id}/archive", response_model=InvoiceProfileResponse)
    def archive_profile(
        ledger_id: UUID, profile_id: UUID, body: InvoiceProfileArchive, identity: writer
    ):
        return execute(service.archive, identity.id, ledger_id, profile_id, body)

    return router
