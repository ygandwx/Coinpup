"""Apply and recover witnessed device commands using the standard authenticated app."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.routing import APIRoute

from coinpup_api.access import BrowserAccess
from coinpup_api.auth import Identity
from coinpup_api.ledger.posting_router import raw_command_body
from coinpup_api.ledger.service import LedgerError
from coinpup_api.sync.financial_push import MAX_FINANCIAL_PUSH_BYTES, _invalid_envelope
from coinpup_api.sync.financial_sync import FinancialSyncService
from coinpup_api.sync.financial_sync_schemas import (
    FinancialConflictKeepCurrent,
    FinancialConflictResolution,
    FinancialPushRequest,
    FinancialPushResponse,
)


def _http_error(error):
    return HTTPException(error.status, {"code": error.code, "message": str(error)})


class _BoundedFinancialPush(APIRoute):
    def get_route_handler(self):
        handle = super().get_route_handler()

        async def bounded(request: Request):
            received = 0

            async def receive():
                nonlocal received
                message = await request.receive()
                if message["type"] == "http.request":
                    received += len(message.get("body", b""))
                    if received > MAX_FINANCIAL_PUSH_BYTES:
                        raise _http_error(_invalid_envelope())
                return message

            # FastAPI still parses the typed body and caches the exact bytes for replay.
            return await handle(Request(request.scope, receive))

        return bounded


def create_financial_sync_router(settings, engine):
    router = APIRouter(
        prefix="/api/v1/sync/devices/{device_id}/financial-pushes", tags=["sync financial pushes"]
    )
    service = FinancialSyncService(engine)
    access = BrowserAccess(settings, engine)
    read = Annotated[Identity, Depends(access.read)]
    write = Annotated[Identity, Depends(access.write)]
    original = Annotated[bytes, Depends(raw_command_body)]

    def call(method, *args):
        try:
            return method(*args)
        except LedgerError as error:
            raise _http_error(error) from None

    def submit_financial_push(
        identity: write,
        device_id: UUID,
        intent_id: UUID,
        body: FinancialPushRequest,
        raw_body: original,
    ):
        """Apply one stable intent or return its immutable applied or conflict outcome."""
        return call(service.submit, identity.id, device_id, intent_id, raw_body)

    router.add_api_route(
        "/{intent_id}",
        submit_financial_push,
        methods=["PUT"],
        response_model=FinancialPushResponse,
        route_class_override=_BoundedFinancialPush,
    )

    @router.get("/{intent_id}", response_model=FinancialPushResponse)
    def get_financial_push(identity: read, device_id: UUID, intent_id: UUID):
        return call(service.get, identity.id, device_id, intent_id)

    @router.put("/{intent_id}/resolution", response_model=FinancialConflictResolution)
    def accept_current_financial_version(
        identity: write, device_id: UUID, intent_id: UUID, body: FinancialConflictKeepCurrent
    ):
        return call(
            service.keep_current, identity.id, device_id, intent_id, body.expected_current_version
        )

    @router.get("/{intent_id}/resolution", response_model=FinancialConflictResolution | None)
    def get_financial_conflict_resolution(identity: read, device_id: UUID, intent_id: UUID):
        return call(service.get_resolution, identity.id, device_id, intent_id)

    return router
