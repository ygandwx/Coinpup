"""Separate OCR envelope; existing financial command schemas are reused unchanged."""

import hashlib
import json
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, Field, StrictBool, ValidationError, field_validator

from coinpup_api.ledger.posting_schemas import (
    ExchangeCreate,
    ExpenseCreate,
    FinancialResponse,
    IncomeCreate,
    OpeningCreate,
    TransferCreate,
)
from coinpup_api.ledger.schemas import Version
from coinpup_api.ledger.service import LedgerError

from .contracts import CANDIDATE_BYTES, InputModel, canonical_json
from .review import HumanReview, ReviewPath


class OpeningEntry(InputModel):
    kind: Literal["opening"]
    command: OpeningCreate


class IncomeEntry(InputModel):
    kind: Literal["income"]
    command: IncomeCreate


class ExpenseEntry(InputModel):
    kind: Literal["expense"]
    command: ExpenseCreate


class TransferEntry(InputModel):
    kind: Literal["transfer"]
    command: TransferCreate


class ExchangeEntry(InputModel):
    kind: Literal["exchange"]
    command: ExchangeCreate


class LinkEntry(InputModel):
    kind: Literal["link"]
    operation_id: UUID
    expected_version: Version


Entry = Annotated[
    OpeningEntry | IncomeEntry | ExpenseEntry | TransferEntry | ExchangeEntry | LinkEntry,
    Field(discriminator="kind"),
]


class ConfirmationCreate(InputModel):
    intent_id: UUID
    expected_version: Version
    target_ledger_id: UUID
    target_file_id: UUID
    confirmed: Annotated[list[ReviewPath], Field(max_length=1000)]
    entry: Entry
    duplicate_ack: StrictBool = False

    @field_validator("confirmed")
    @classmethod
    def unique_paths(cls, value):
        return HumanReview(confirmed=value).confirmed


class ConfirmationResponse(BaseModel):
    intent_id: UUID
    draft_id: UUID
    ledger_id: UUID
    draft_version: int
    action: Literal["create", "link"]
    target_ledger_id: UUID
    target_file_id: UUID
    operation: FinancialResponse


def confirmation_request(ledger_id, draft_id, payload, raw_body=None):
    try:
        provided = (
            payload.model_dump(mode="json", exclude_unset=True)
            if raw_body is None
            else json.loads(raw_body)
        )
        canonical_json(provided, CANDIDATE_BYTES)
        if ConfirmationCreate.model_validate(provided) != payload:
            raise ValueError
        encoded = canonical_json(
            {
                "action": "ocr-confirm",
                "route": "/api/v1/ledgers/{ledger_id}/ocr-drafts/{draft_id}/confirmations",
                "ledger_id": str(ledger_id),
                "draft_id": str(draft_id),
                "payload": provided,
            },
            CANDIDATE_BYTES,
        )
        return provided, hashlib.sha256(encoded.encode("utf-8")).hexdigest()
    except (ValueError, TypeError, RecursionError, ValidationError):
        raise LedgerError("ocr_invalid_payload", 422, "The confirmation body is invalid.") from None


def command_assets(entry):
    if entry.kind == "link":
        return []
    command = entry.command
    assets = {
        getattr(command, name)
        for name in ("asset_id", "source_asset_id", "destination_asset_id")
        if hasattr(command, name)
    }
    assets.update(fee.asset_id for fee in getattr(command, "fees", []))
    return sorted(assets)
