"""Owned recurring rules and immutable instances; generation remains a maintenance job."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Path, Query
from sqlalchemy import Engine

from coinpup_api.access import BrowserAccess
from coinpup_api.auth import Identity
from coinpup_api.business.recurring_generation import RecurringGenerationService
from coinpup_api.business.recurring_schemas import (
    RecurringInstanceResponse,
    RecurringRuleArchive,
    RecurringRuleCreate,
    RecurringRuleResponse,
    RecurringRuleUpdate,
)
from coinpup_api.config import Settings
from coinpup_api.ledger.service import LedgerError


def create_recurring_router(settings: Settings, engine: Engine | None) -> APIRouter:
    router = APIRouter(
        prefix="/api/v1/ledgers/{ledger_id}/recurring-invoice-rules",
        tags=["recurring invoice drafts"],
    )
    service, access = RecurringGenerationService(engine), BrowserAccess(settings, engine)
    reader = Annotated[Identity, Depends(access.read)]
    writer = Annotated[Identity, Depends(access.write)]
    page_size = Annotated[int, Query(ge=1, le=200)]
    page_offset = Annotated[int, Query(ge=0, le=100000)]
    occurrence = Annotated[int, Path(ge=0, lt=2147483647)]

    def execute(operation, *args, **kwargs):
        try:
            return operation(*args, **kwargs)
        except LedgerError as error:
            raise HTTPException(error.status, {"code": error.code, "message": str(error)}) from None

    @router.get("", response_model=list[RecurringRuleResponse])
    def list_rules(
        ledger_id: UUID,
        identity: reader,
        include_archived: bool = True,
        limit: page_size = 100,
        offset: page_offset = 0,
    ):
        return execute(
            service.list_rules,
            identity.id,
            ledger_id,
            include_archived=include_archived,
            limit=limit,
            offset=offset,
        )

    @router.post("", response_model=RecurringRuleResponse, status_code=201)
    def create_rule(ledger_id: UUID, body: RecurringRuleCreate, identity: writer):
        return execute(service.create_rule, identity.id, ledger_id, body)

    @router.get("/{rule_id}", response_model=RecurringRuleResponse)
    def get_rule(ledger_id: UUID, rule_id: UUID, identity: reader):
        return execute(service.get_rule, identity.id, ledger_id, rule_id)

    @router.patch("/{rule_id}", response_model=RecurringRuleResponse)
    def update_rule(ledger_id: UUID, rule_id: UUID, body: RecurringRuleUpdate, identity: writer):
        return execute(service.update_rule, identity.id, ledger_id, rule_id, body)

    @router.patch("/{rule_id}/archive", response_model=RecurringRuleResponse)
    def archive_rule(ledger_id: UUID, rule_id: UUID, body: RecurringRuleArchive, identity: writer):
        return execute(service.archive_rule, identity.id, ledger_id, rule_id, body)

    @router.get("/{rule_id}/instances", response_model=list[RecurringInstanceResponse])
    def list_instances(
        ledger_id: UUID,
        rule_id: UUID,
        identity: reader,
        limit: page_size = 100,
        offset: page_offset = 0,
    ):
        return execute(
            service.list_instances, identity.id, ledger_id, rule_id, limit=limit, offset=offset
        )

    @router.get("/{rule_id}/instances/{index}", response_model=RecurringInstanceResponse)
    def get_instance(ledger_id: UUID, rule_id: UUID, index: occurrence, identity: reader):
        return execute(service.get_instance, identity.id, ledger_id, rule_id, index)

    return router
