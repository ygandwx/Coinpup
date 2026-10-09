"""Owned business drafts share the existing browser authentication boundary."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import Engine

from coinpup_api.access import BrowserAccess
from coinpup_api.auth import Identity
from coinpup_api.business.draft_schemas import (
    BusinessDraftArchive,
    BusinessDraftCreate,
    BusinessDraftResponse,
    BusinessDraftSummary,
    BusinessDraftUpdate,
)
from coinpup_api.business.drafts import DraftService
from coinpup_api.config import Settings
from coinpup_api.ledger.service import LedgerError


def create_business_draft_router(settings: Settings, engine: Engine | None) -> APIRouter:
    router = APIRouter(
        prefix="/api/v1/ledgers/{ledger_id}/business-documents", tags=["business drafts"]
    )
    service, access = DraftService(engine), BrowserAccess(settings, engine)
    reader = Annotated[Identity, Depends(access.read)]
    writer = Annotated[Identity, Depends(access.write)]
    page_size = Annotated[int, Query(ge=1, le=200)]
    page_offset = Annotated[int, Query(ge=0, le=100000)]

    def execute(operation, *args, **kwargs):
        try:
            return operation(*args, **kwargs)
        except LedgerError as error:
            raise HTTPException(error.status, {"code": error.code, "message": str(error)}) from None

    @router.get("", response_model=list[BusinessDraftSummary])
    def list_business_documents(
        ledger_id: UUID,
        identity: reader,
        include_archived: bool = True,
        limit: page_size = 100,
        offset: page_offset = 0,
    ):
        return execute(
            service.list_drafts,
            identity.id,
            ledger_id,
            include_archived=include_archived,
            limit=limit,
            offset=offset,
        )

    @router.get("/{document_id}", response_model=BusinessDraftResponse)
    def get_business_document(ledger_id: UUID, document_id: UUID, identity: reader):
        return execute(service.get_draft, identity.id, ledger_id, document_id)

    @router.post("", response_model=BusinessDraftResponse, status_code=201)
    def create_business_document(ledger_id: UUID, body: BusinessDraftCreate, identity: writer):
        return execute(service.create_draft, identity.id, ledger_id, body)

    @router.put("/{document_id}", response_model=BusinessDraftResponse)
    def update_business_document(
        ledger_id: UUID, document_id: UUID, body: BusinessDraftUpdate, identity: writer
    ):
        return execute(service.update_draft, identity.id, ledger_id, document_id, body)

    @router.patch("/{document_id}/archive", response_model=BusinessDraftResponse)
    def archive_business_document(
        ledger_id: UUID, document_id: UUID, body: BusinessDraftArchive, identity: writer
    ):
        return execute(service.set_draft_archived, identity.id, ledger_id, document_id, body)

    return router
