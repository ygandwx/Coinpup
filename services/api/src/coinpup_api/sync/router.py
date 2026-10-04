"""Authenticated, bounded reads of the owner's ordered change notifications."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import Engine

from coinpup_api.access import BrowserAccess
from coinpup_api.auth import Identity
from coinpup_api.config import Settings
from coinpup_api.ledger.service import LedgerError
from coinpup_api.sync.schemas import ChangePage, Cursor
from coinpup_api.sync.service import ChangeService


def create_sync_router(settings: Settings, engine: Engine | None) -> APIRouter:
    router = APIRouter(prefix="/api/v1", tags=["change notifications"])
    service = ChangeService(engine)
    access = BrowserAccess(settings, engine)
    reader = Annotated[Identity, Depends(access.read)]
    page_size = Annotated[int, Query(ge=1, le=200)]

    @router.get("/changes", response_model=ChangePage)
    def changes(identity: reader, after: Cursor = "0", limit: page_size = 100):
        """Read notifications after an exact cursor; an empty page retains that cursor."""
        try:
            return service.list_changes(identity.id, after=after, limit=limit)
        except LedgerError as error:
            raise HTTPException(error.status, {"code": error.code, "message": str(error)}) from None

    return router
