"""Wire contracts for permanent device push outcomes and an explicit conflict decision."""

from typing import Annotated, Literal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StrictInt, StrictStr

from coinpup_api.ledger.posting_schemas import FinancialResponse, HistoryEntry, OperationState
from coinpup_api.sync.financial_push import FinancialPushCommand
from coinpup_api.sync.schemas import DeviceRegistration

PushKey = Annotated[StrictStr, Field(min_length=1, max_length=128, pattern=r"^[!-~]{1,128}$")]


class FinancialPushRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    ledger_id: UUID
    idempotency_key: PushKey
    checkpoint: DeviceRegistration
    command: FinancialPushCommand


class _PushOutcome(BaseModel):
    intent_id: UUID
    device_id: UUID
    ledger_id: UUID
    operation_id: UUID


class FinancialPushApplied(_PushOutcome):
    outcome: Literal["applied"]
    receipt: FinancialResponse | OperationState


class FinancialPushConflict(_PushOutcome):
    outcome: Literal["conflict"]
    command_kind: Literal["correct", "cancel"]
    original_payload: str
    basis: HistoryEntry
    competing: HistoryEntry


FinancialPushResponse = Annotated[
    FinancialPushApplied | FinancialPushConflict, Field(discriminator="outcome")
]


class FinancialConflictKeepCurrent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_current_version: Annotated[StrictInt, Field(ge=1, le=2_147_483_647)]


class FinancialConflictResolution(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    intent_id: UUID
    device_id: UUID
    accepted_version: int
    resolved_at: AwareDatetime
