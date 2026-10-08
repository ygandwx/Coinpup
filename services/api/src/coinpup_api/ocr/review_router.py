"""Authenticated manual work in progress; saving a review never posts money."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.exc import SQLAlchemyError

from coinpup_api.access import BrowserAccess
from coinpup_api.auth import Identity
from coinpup_api.ledger.service import LedgerError

from .review import DraftReviewService, DraftReviewUpdate, ReviewView


def create_review_router(settings, engine):
    router = APIRouter(prefix="/api/v1/ledgers/{ledger_id}/ocr-drafts", tags=["OCR drafts"])
    service, access = DraftReviewService(engine), BrowserAccess(settings, engine)
    reader = Annotated[Identity, Depends(access.read)]
    writer = Annotated[Identity, Depends(access.write)]

    def execute(operation, *args):
        try:
            return operation(*args)
        except LedgerError as error:
            raise HTTPException(error.status, {"code": error.code, "message": str(error)}) from None
        except SQLAlchemyError:
            raise HTTPException(
                503, {"code": "ocr_unavailable", "message": "OCR is unavailable."}
            ) from None

    @router.get("/{draft_id}/review", response_model=ReviewView)
    def get_review(ledger_id: UUID, draft_id: UUID, identity: reader):
        return execute(service.get_review, identity.id, ledger_id, draft_id)

    @router.put("/{draft_id}/review", response_model=ReviewView)
    def update_review(
        ledger_id: UUID, draft_id: UUID, payload: DraftReviewUpdate, identity: writer
    ):
        return execute(service.update_review, identity.id, ledger_id, draft_id, payload)

    return router
