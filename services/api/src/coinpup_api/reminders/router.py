"""Authenticated reminder reads and explicit versioned state transitions."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import Engine

from coinpup_api.access import BrowserAccess
from coinpup_api.auth import Identity
from coinpup_api.config import Settings
from coinpup_api.ledger.service import LedgerError
from coinpup_api.reminders.schemas import (
    ReminderCreate,
    ReminderEdit,
    ReminderManual,
    ReminderRecalculate,
    ReminderResponse,
    ReminderRevisionResponse,
    ReminderRuleResponse,
    ReminderTransition,
)
from coinpup_api.reminders.service import ReminderService


def create_reminder_router(settings: Settings, engine: Engine | None) -> APIRouter:
    router = APIRouter(prefix="/api/v1/ledgers/{ledger_id}/reminders", tags=["reminders"])
    service, access = ReminderService(engine), BrowserAccess(settings, engine)
    reader = Annotated[Identity, Depends(access.read)]
    writer = Annotated[Identity, Depends(access.write)]
    page_size = Annotated[int, Query(ge=1, le=200)]
    page_offset = Annotated[int, Query(ge=0, le=100000)]

    def execute(operation, *args, **kwargs):
        try:
            return operation(*args, **kwargs)
        except LedgerError as error:
            raise HTTPException(error.status, {"code": error.code, "message": str(error)}) from None

    @router.get("", response_model=list[ReminderResponse])
    def list_events(
        ledger_id: UUID,
        identity: reader,
        include_archived: bool = True,
        include_completed: bool = True,
        limit: page_size = 100,
        offset: page_offset = 0,
    ):
        return execute(
            service.list_events,
            identity.id,
            ledger_id,
            include_archived=include_archived,
            include_completed=include_completed,
            limit=limit,
            offset=offset,
        )

    @router.post("", response_model=ReminderResponse, status_code=201)
    def create_event(ledger_id: UUID, body: ReminderCreate, identity: writer):
        return execute(service.create_event, identity.id, ledger_id, body)

    @router.get("/rules", response_model=list[ReminderRuleResponse])
    def list_rules(ledger_id: UUID, identity: reader):
        return execute(service.list_rules, identity.id, ledger_id)

    @router.get("/{event_id}", response_model=ReminderResponse)
    def get_event(ledger_id: UUID, event_id: UUID, identity: reader):
        return execute(service.get_event, identity.id, ledger_id, event_id)

    @router.patch("/{event_id}", response_model=ReminderResponse)
    def edit_event(ledger_id: UUID, event_id: UUID, body: ReminderEdit, identity: writer):
        return execute(service.edit_event, identity.id, ledger_id, event_id, body)

    @router.post("/{event_id}/recalculate", response_model=ReminderResponse)
    def recalculate_event(
        ledger_id: UUID, event_id: UUID, body: ReminderRecalculate, identity: writer
    ):
        return execute(service.recalculate_event, identity.id, ledger_id, event_id, body)

    @router.patch("/{event_id}/manual-date", response_model=ReminderResponse)
    def manual_date(ledger_id: UUID, event_id: UUID, body: ReminderManual, identity: writer):
        return execute(service.set_manual_date, identity.id, ledger_id, event_id, body)

    @router.post("/{event_id}/transition", response_model=ReminderResponse)
    def transition_event(
        ledger_id: UUID, event_id: UUID, body: ReminderTransition, identity: writer
    ):
        return execute(service.transition_event, identity.id, ledger_id, event_id, body)

    @router.get("/{event_id}/revisions", response_model=list[ReminderRevisionResponse])
    def list_revisions(
        ledger_id: UUID,
        event_id: UUID,
        identity: reader,
        limit: page_size = 100,
        offset: page_offset = 0,
    ):
        return execute(
            service.list_revisions, identity.id, ledger_id, event_id, limit=limit, offset=offset
        )

    return router
