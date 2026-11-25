"""Authenticated, bounded reads of the owner's ordered change notifications."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import Engine

from coinpup_api.access import BrowserAccess
from coinpup_api.auth import Identity
from coinpup_api.config import Settings
from coinpup_api.ledger.service import LedgerError
from coinpup_api.sync.schemas import ChangePage, ChangeRecord, ChangeStart, Cursor, SequenceText
from coinpup_api.sync.service import ChangeService


def create_sync_router(settings: Settings, engine: Engine | None) -> APIRouter:
    router = APIRouter(prefix="/api/v1", tags=["change notifications"])
    service = ChangeService(engine)
    access = BrowserAccess(settings, engine)
    reader = Annotated[Identity, Depends(access.read)]
    page_size = Annotated[int, Query(ge=1, le=200)]

    @router.get("/changes", response_model=ChangePage)
    def changes(
        identity: reader, after: Cursor = "0", limit: page_size = 100, through: Cursor | None = None
    ):
        """Read after a cursor, optionally up to an inclusive saved boundary."""
        try:
            return service.list_changes(identity.id, after=after, limit=limit, through=through)
        except LedgerError as error:
            raise HTTPException(error.status, {"code": error.code, "message": str(error)}) from None

    @router.get("/changes/start", response_model=ChangeStart)
    def start(identity: reader):
        """Capture the owner’s committed cursor before reading the first full snapshot."""
        try:
            return service.start_cursor(identity.id)
        except LedgerError as error:
            raise HTTPException(error.status, {"code": error.code, "message": str(error)}) from None

    @router.get("/changes/at/{seq}", response_model=ChangeRecord)
    def at(identity: reader, seq: SequenceText):
        """Read one exact committed notification as evidence for resuming a device cursor."""
        try:
            return service.change_at(identity.id, seq)
        except LedgerError as error:
            raise HTTPException(error.status, {"code": error.code, "message": str(error)}) from None

    return router
