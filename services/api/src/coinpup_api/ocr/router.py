"""Authenticated task metadata commands; processing remains a private worker concern."""

from copy import deepcopy
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import Engine
from sqlalchemy.exc import SQLAlchemyError

from coinpup_api.access import BrowserAccess
from coinpup_api.auth import Identity
from coinpup_api.config import Settings
from coinpup_api.ledger.service import LedgerError
from coinpup_api.ocr.contracts import JobCreate, JobView
from coinpup_api.ocr.queue import OcrQueueService
from coinpup_api.ocr.schemas import JobRetry


def _unavailable():
    return HTTPException(503, {"code": "ocr_unavailable", "message": "OCR is unavailable."})


def create_ocr_router(settings: Settings, engine: Engine | None, configuration=None) -> APIRouter:
    router = APIRouter(prefix="/api/v1/ledgers/{ledger_id}/ocr-jobs", tags=["OCR tasks"])
    service = OcrQueueService(engine)
    # Do not validate here: stored intents remain replayable with absent or changed settings.
    frozen_configuration = deepcopy(configuration)
    access = BrowserAccess(settings, engine)
    reader = Annotated[Identity, Depends(access.read)]
    writer = Annotated[Identity, Depends(access.write)]
    page_size = Annotated[int, Query(ge=1, le=200)]
    page_offset = Annotated[int, Query(ge=0, le=100000)]

    def execute(operation, *args, creating=False, **kwargs):
        try:
            return operation(*args, **kwargs)
        except LedgerError as error:
            if (
                creating
                and frozen_configuration is None
                and error.code == "ocr_invalid_configuration"
            ):
                raise _unavailable() from None
            raise HTTPException(error.status, {"code": error.code, "message": str(error)}) from None
        except SQLAlchemyError:
            raise _unavailable() from None

    @router.post("", response_model=JobView, status_code=201)
    def create_job(ledger_id: UUID, body: JobCreate, identity: writer):
        """Create a stable processing intent or return its existing task state."""
        return execute(
            service.create_job,
            identity.id,
            ledger_id,
            body,
            configuration=frozen_configuration,
            creating=True,
        )

    @router.get("", response_model=list[JobView])
    def list_jobs(
        ledger_id: UUID,
        identity: reader,
        intent_id: UUID | None = None,
        state: Literal["pending", "running", "succeeded", "failed"] | None = None,
        limit: page_size = 100,
        offset: page_offset = 0,
    ):
        """Look up an unknown create result by its original intent, or page through tasks."""
        return execute(
            service.list_jobs,
            identity.id,
            ledger_id,
            intent_id=intent_id,
            state=state,
            limit=limit,
            offset=offset,
        )

    @router.get("/{job_id}", response_model=JobView)
    def get_job(ledger_id: UUID, job_id: UUID, identity: reader):
        return execute(service.get_job, identity.id, ledger_id, job_id)

    @router.post("/{job_id}/retries", response_model=JobView)
    def retry_job(ledger_id: UUID, job_id: UUID, body: JobRetry, identity: writer):
        """Requeue a failed task within its original attempt budget."""
        return execute(service.retry_job, identity.id, ledger_id, job_id, body.expected_version)

    return router
