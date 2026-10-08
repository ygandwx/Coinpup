"""Read-only owner-scoped control balances, separate from legacy money routes."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query

from coinpup_api.access import BrowserAccess
from coinpup_api.auth import Identity
from coinpup_api.ledger.control_accounts import ControlAccounts
from coinpup_api.ledger.control_schemas import ControlBalance, ControlClass, ControlKey
from coinpup_api.ledger.service import LedgerError


def create_control_router(settings, engine):
    router = APIRouter(prefix="/api/v1/ledgers/{ledger_id}", tags=["control balances"])
    service = ControlAccounts(engine)
    access = BrowserAccess(settings, engine)
    reader = Annotated[Identity, Depends(access.read)]

    @router.get("/control-balances", response_model=list[ControlBalance])
    def balances(
        ledger_id: UUID,
        identity: reader,
        account_class: ControlClass | None = None,
        system_key: ControlKey | None = None,
        party_id: UUID | None = None,
        counterparty_entity_id: UUID | None = None,
        document_id: UUID | None = None,
        document_line_id: UUID | None = None,
        limit: Annotated[int, Query(ge=1, le=200)] = 100,
        offset: Annotated[int, Query(ge=0, le=100000)] = 0,
    ):
        try:
            return service.balances(
                identity.id,
                ledger_id,
                account_class=account_class,
                system_key=system_key,
                party_id=party_id,
                counterparty_entity_id=counterparty_entity_id,
                document_id=document_id,
                document_line_id=document_line_id,
                limit=limit,
                offset=offset,
            )
        except LedgerError as error:
            raise HTTPException(error.status, {"code": error.code, "message": str(error)}) from None

    return router
