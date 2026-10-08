"""Owner/ledger-scoped draft evidence reads; no lease or blob paths leave this boundary."""

from datetime import datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import and_, select
from sqlalchemy.exc import SQLAlchemyError

from coinpup_api.access import BrowserAccess
from coinpup_api.auth import Identity
from coinpup_api.ledger.service import LedgerError, LedgerService, _not_found, _page

from .models import OcrDraft, OcrJob


class DraftSummary(BaseModel):
    id: UUID
    ledger_id: UUID
    job_id: UUID
    file_id: UUID
    source_key: str
    status: Literal["draft", "ignored"]
    version: int
    created_at: datetime
    updated_at: datetime


class DraftDetail(DraftSummary):
    recognized: dict[str, Any]
    evidence: dict[str, Any]
    fields: dict[str, Any]
    recognition: dict[str, Any] | None
    selection: dict[str, Any] | None


def _query(owner_id, ledger_id, *, detail=False):
    columns = [getattr(OcrDraft, name) for name in DraftSummary.model_fields if name != "file_id"]
    columns.append(OcrJob.file_id)
    if detail:
        columns.extend(
            [
                OcrDraft.recognized,
                OcrDraft.evidence,
                OcrDraft.fields,
                OcrJob.result,
                OcrJob.configuration,
            ]
        )
    return (
        select(*columns)
        .join(
            OcrJob,
            and_(
                OcrJob.id == OcrDraft.job_id,
                OcrJob.ledger_id == OcrDraft.ledger_id,
                OcrJob.created_by == OcrDraft.created_by,
            ),
        )
        .where(OcrDraft.created_by == owner_id, OcrDraft.ledger_id == ledger_id)
    )


class DraftReadService(LedgerService):
    def list_drafts(self, owner_id, ledger_id, *, job_id=None, status=None, limit=100, offset=0):
        _page(limit, offset)
        if status is not None and status not in ("draft", "ignored"):
            raise LedgerError("ocr_invalid_filter", 422, "The draft filter is invalid.")
        with self._transaction(owner_id, read_only=True) as session:
            self._ledger(session, owner_id, ledger_id)
            query = _query(owner_id, ledger_id)
            if job_id is not None:
                query = query.where(OcrDraft.job_id == job_id)
            if status is not None:
                query = query.where(OcrDraft.status == status)
            return [
                DraftSummary.model_validate(row)
                for row in session.execute(
                    query.order_by(OcrDraft.created_at, OcrDraft.id).limit(limit).offset(offset)
                ).mappings()
            ]

    def get_draft(self, owner_id, ledger_id, draft_id):
        with self._transaction(owner_id, read_only=True) as session:
            self._ledger(session, owner_id, ledger_id)
            row = (
                session.execute(
                    _query(owner_id, ledger_id, detail=True).where(OcrDraft.id == draft_id)
                )
                .mappings()
                .one_or_none()
            )
            if row is None:
                raise _not_found()
            values = dict(row)
            result, config = values.pop("result"), values.pop("configuration")
            summary = result.get("summary", {}) if isinstance(result, dict) else {}
            processing = config.get("processing", {})
            recognition = summary.get("recognition") if isinstance(summary, dict) else None
            selection = processing.get("selection") if isinstance(processing, dict) else None
            return DraftDetail(
                **values,
                recognition=recognition if isinstance(recognition, dict) else None,
                selection=selection if isinstance(selection, dict) else None,
            )


def create_draft_router(settings, engine):
    router = APIRouter(prefix="/api/v1/ledgers/{ledger_id}/ocr-drafts", tags=["OCR drafts"])
    service = DraftReadService(engine)
    access = BrowserAccess(settings, engine)
    reader = Annotated[Identity, Depends(access.read)]
    page_size = Annotated[int, Query(ge=1, le=200)]
    page_offset = Annotated[int, Query(ge=0, le=100000)]

    def execute(operation, *args, **kwargs):
        try:
            return operation(*args, **kwargs)
        except LedgerError as error:
            raise HTTPException(error.status, {"code": error.code, "message": str(error)}) from None
        except SQLAlchemyError:
            raise HTTPException(
                503, {"code": "ocr_unavailable", "message": "OCR is unavailable."}
            ) from None

    @router.get("", response_model=list[DraftSummary])
    def list_drafts(
        ledger_id: UUID,
        identity: reader,
        job_id: UUID | None = None,
        status: Literal["draft", "ignored"] | None = None,
        limit: page_size = 100,
        offset: page_offset = 0,
    ):
        """List bounded metadata; private text is available only in an authenticated detail."""
        return execute(
            service.list_drafts,
            identity.id,
            ledger_id,
            job_id=job_id,
            status=status,
            limit=limit,
            offset=offset,
        )

    @router.get("/{draft_id}", response_model=DraftDetail)
    def get_draft(ledger_id: UUID, draft_id: UUID, identity: reader):
        """Read original evidence and current review fields, including archived sources."""
        return execute(service.get_draft, identity.id, ledger_id, draft_id)

    return router
