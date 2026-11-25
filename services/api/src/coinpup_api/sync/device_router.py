"""Register the session owner's device or read its immutable history; no sync progress writes."""

from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import AwareDatetime, BaseModel, Field

from coinpup_api.access import BrowserAccess
from coinpup_api.auth import Identity
from coinpup_api.ledger.service import LedgerError
from coinpup_api.sync.devices import DeviceRegistry
from coinpup_api.sync.schemas import ChangeStart, DeviceRegistration


class DeviceRecord(BaseModel):
    owner_id: UUID
    device_id: UUID
    version: Literal[1, 2]
    state: Literal["registered", "retired"]
    recorded_at: AwareDatetime
    start: ChangeStart


class DeviceHistory(BaseModel):
    revisions: Annotated[list[DeviceRecord], Field(min_length=1, max_length=2)]


def create_device_router(settings, engine):
    router = APIRouter(prefix="/api/v1/sync/devices", tags=["sync devices"])
    svc = DeviceRegistry(engine)
    access = BrowserAccess(settings, engine)
    reader = Annotated[Identity, Depends(access.read)]
    writer = Annotated[Identity, Depends(access.write)]

    def call(fn, *args):
        try:
            return fn(*args)
        except LedgerError as e:
            raise HTTPException(e.status, {"code": e.code, "message": str(e)}) from None

    @router.put("/{device_id}", response_model=DeviceRecord)
    def register(identity: writer, device_id: UUID, body: DeviceRegistration | None = None):
        """Return the current state. Original registration does not undo retirement."""
        return call(svc.register, identity.id, device_id, body)

    @router.put("/{device_id}/retirement", response_model=DeviceRecord)
    def retire(identity: writer, device_id: UUID):
        """Record one terminal retirement; an identical request returns the original facts."""
        return call(svc.retire, identity.id, device_id)

    @router.get("/{device_id}/history", response_model=DeviceHistory)
    def history(identity: reader, device_id: UUID):
        return {"revisions": call(svc.history, identity.id, device_id)}

    return router
