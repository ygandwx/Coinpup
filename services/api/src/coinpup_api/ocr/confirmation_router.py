"""Cookie-protected confirmation boundary preserves the original request bytes."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.exc import SQLAlchemyError

from coinpup_api.access import BrowserAccess
from coinpup_api.auth import Identity
from coinpup_api.ledger.posting_router import raw_command_body
from coinpup_api.ledger.service import LedgerError

from .confirmation import ConfirmationService
from .confirmation_contracts import ConfirmationCreate, ConfirmationResponse
from .confirmation_reads import ConfirmationReadService, DuplicateConfirmation


def create_confirmation_router(settings, engine):
    router = APIRouter(prefix="/api/v1/ledgers/{ledger_id}/ocr-drafts", tags=["OCR drafts"])
    service, reads = ConfirmationService(engine), ConfirmationReadService(engine)
    access = BrowserAccess(settings, engine)
    reader = Annotated[Identity, Depends(access.read)]
    writer = Annotated[Identity, Depends(access.write)]
    original_body = Annotated[bytes, Depends(raw_command_body)]

    def execute(operation, *args, **kwargs):
        try:
            return operation(*args, **kwargs)
        except LedgerError as error:
            raise HTTPException(error.status, {"code": error.code, "message": str(error)}) from None
        except SQLAlchemyError:
            raise HTTPException(
                503, {"code": "ocr_unavailable", "message": "OCR is unavailable."}
            ) from None

    @router.post("/{draft_id}/confirmations", response_model=ConfirmationResponse, status_code=201)
    def confirm(
        ledger_id: UUID,
        draft_id: UUID,
        payload: ConfirmationCreate,
        identity: writer,
        raw_body: original_body,
    ):
        """Retry with the same intent and original JSON; changed requests return conflict."""
        return execute(
            service.confirm, identity.id, ledger_id, draft_id, payload, raw_body=raw_body
        )

    @router.get("/{draft_id}/confirmation", response_model=ConfirmationResponse)
    def get_confirmation(ledger_id: UUID, draft_id: UUID, identity: reader):
        """Read the historical receipt; unknown submissions should replay the original POST."""
        return execute(reads.get_confirmation, identity.id, ledger_id, draft_id)

    @router.get("/{draft_id}/duplicates", response_model=list[DuplicateConfirmation])
    def duplicates(ledger_id: UUID, draft_id: UUID, identity: reader):
        """Up to 20 prior confirmations of the same bytes/source row; never auto-merge."""
        return execute(reads.list_duplicates, identity.id, ledger_id, draft_id)

    return router
