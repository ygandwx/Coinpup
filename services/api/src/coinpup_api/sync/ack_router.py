"""Session-owned page acknowledgements; the latest cursor is read independently of replay."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StrictInt

from coinpup_api.access import BrowserAccess
from coinpup_api.auth import Identity
from coinpup_api.ledger.service import LedgerError
from coinpup_api.sync.acknowledgements import DeviceAcknowledgements
from coinpup_api.sync.schemas import ChangePage, ChangeStart, Cursor, SavedNotice, SequenceText

PageSize = Annotated[StrictInt, Field(ge=1, le=200)]


class ReceivedPage(ChangePage):
    model_config = ConfigDict(extra="forbid")
    changes: Annotated[list[SavedNotice], Field(max_length=200)]


class PageAcknowledgementRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    after: Cursor
    through: Cursor
    limit: PageSize
    prior: SavedNotice | None
    received: ReceivedPage


class PageReceipt(BaseModel):
    owner_id: UUID
    device_id: UUID
    after: Cursor
    through: SequenceText
    limit: PageSize
    count: PageSize
    cursor: SequenceText
    recorded_at: AwareDatetime


def create_ack_router(settings, engine):
    router = APIRouter(prefix="/api/v1/sync/devices", tags=["sync devices"])
    service = DeviceAcknowledgements(engine)
    access = BrowserAccess(settings, engine)
    reader = Annotated[Identity, Depends(access.read)]
    writer = Annotated[Identity, Depends(access.write)]

    @router.post("/{device_id}/acknowledgements", response_model=PageReceipt)
    def acknowledge(identity: writer, device_id: UUID, body: PageAcknowledgementRequest):
        """Compare the complete saved page. A replay returns its original receipt."""
        try:
            return service.acknowledge(
                identity.id,
                device_id,
                after=body.after,
                through=body.through,
                limit=body.limit,
                prior=body.prior,
                received=body.received,
            )
        except LedgerError as error:
            raise HTTPException(error.status, {"code": error.code, "message": str(error)}) from None

    @router.get("/{device_id}/acknowledgements/latest", response_model=PageReceipt | None)
    def latest(identity: reader, device_id: UUID):
        """Return null for a known device without an acknowledgement."""
        try:
            return service.latest(identity.id, device_id)
        except LedgerError as error:
            raise HTTPException(error.status, {"code": error.code, "message": str(error)}) from None

    @router.get("/{device_id}/checkpoint", response_model=ChangeStart)
    def checkpoint(identity: reader, device_id: UUID):
        try:
            return service.checkpoint(identity.id, device_id)
        except LedgerError as error:
            raise HTTPException(error.status, {"code": error.code, "message": str(error)}) from None

    return router
