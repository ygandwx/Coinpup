"""Freeze a typed financial push without changing the existing command identity."""

import json
from dataclasses import dataclass
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from coinpup_api.ledger.idempotency import command_hash_v2, revision_hash_v2
from coinpup_api.ledger.posting_schemas import (
    CancellationCreate,
    CorrectionCreate,
    ExchangeCreate,
    ExpenseCreate,
    IncomeCreate,
    OpeningCreate,
    TransferCreate,
)
from coinpup_api.ledger.service import LedgerError

MAX_FINANCIAL_PUSH_BYTES = 131_072


class PushOpeningPayload(OpeningCreate):
    id: UUID


class PushIncomePayload(IncomeCreate):
    id: UUID


class PushExpensePayload(ExpenseCreate):
    id: UUID


class PushTransferPayload(TransferCreate):
    id: UUID


class PushExchangePayload(ExchangeCreate):
    id: UUID


class _PushCommand(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class PushOpening(_PushCommand):
    kind: Literal["opening"]
    payload: PushOpeningPayload


class PushIncome(_PushCommand):
    kind: Literal["income"]
    payload: PushIncomePayload


class PushExpense(_PushCommand):
    kind: Literal["expense"]
    payload: PushExpensePayload


class PushTransfer(_PushCommand):
    kind: Literal["transfer"]
    payload: PushTransferPayload


class PushExchange(_PushCommand):
    kind: Literal["exchange"]
    payload: PushExchangePayload


class PushCorrection(_PushCommand):
    kind: Literal["correct"]
    operation_id: UUID
    payload: CorrectionCreate


class PushCancellation(_PushCommand):
    kind: Literal["cancel"]
    operation_id: UUID
    payload: CancellationCreate


FinancialPushCommand = Annotated[
    PushOpening
    | PushIncome
    | PushExpense
    | PushTransfer
    | PushExchange
    | PushCorrection
    | PushCancellation,
    Field(discriminator="kind"),
]
_COMMAND = TypeAdapter(FinancialPushCommand)


@dataclass(frozen=True, slots=True)
class FrozenFinancialPush:
    ledger_id: UUID
    operation_id: UUID
    command: FinancialPushCommand
    original_payload: bytes
    ledger_digest: str

    @property
    def expected_version(self) -> int | None:
        if isinstance(self.command, (PushCorrection, PushCancellation)):
            return self.command.payload.expected_version
        return None


def _invalid_envelope() -> LedgerError:
    return LedgerError("invalid_financial_push", 422, "Expected a bounded financial push command.")


def _unique_object(items):
    result = {}
    for key, value in items:
        if key in result:
            raise ValueError("Duplicate JSON member.")
        result[key] = value
    return result


def _reject_constant(_):
    raise ValueError("Non-finite JSON number.")


def decode_push_json(body: bytes) -> dict:
    """Decode the whole envelope once, preserving raw nested field presence and list order."""
    if type(body) is not bytes or not 0 < len(body) <= MAX_FINANCIAL_PUSH_BYTES:
        raise _invalid_envelope()
    try:
        value = json.loads(
            body.decode("utf-8"), object_pairs_hook=_unique_object, parse_constant=_reject_constant
        )
        if not isinstance(value, dict):
            raise ValueError("Expected a JSON object.")
    except (ValueError, RecursionError):
        raise _invalid_envelope() from None
    return value


def freeze_financial_push(ledger_id: UUID, original_command: dict) -> FrozenFinancialPush:
    """Use the original nested payload, rather than the normalized request model, for v2."""
    try:
        command = _COMMAND.validate_python(original_command)
        payload = json.dumps(
            original_command["payload"],
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (ValueError, TypeError, RecursionError):
        raise _invalid_envelope() from None

    if isinstance(command, (PushCorrection, PushCancellation)):
        operation_id = command.operation_id
        digest = revision_hash_v2(
            command.kind, ledger_id, operation_id, command.payload, raw_body=payload
        )
    else:
        operation_id = command.payload.id
        digest = command_hash_v2(command.kind, ledger_id, command.payload, raw_body=payload)
    return FrozenFinancialPush(ledger_id, operation_id, command, payload, digest)
