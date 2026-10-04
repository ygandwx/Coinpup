"""Bounded queue inputs and private immutable worker leases; no engine assumptions."""

import hashlib
import json
import math
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictStr, field_validator

from coinpup_api.ledger.service import LedgerError

CONFIG_BYTES = 16384
CANDIDATE_BYTES = 262144
RESULT_BYTES = 1048576


def _json_size(value):
    """Conservative UTF-8 jsonb::text size, including spaces and expanded exponents."""
    if type(value) is dict:
        if any(type(key) is not str for key in value):
            raise ValueError
        return (
            2
            + max(0, len(value) - 1) * 2
            + sum(_json_size(key) + 2 + _json_size(item) for key, item in value.items())
        )
    if type(value) is list:
        return 2 + max(0, len(value) - 1) * 2 + sum(map(_json_size, value))
    if type(value) is str:
        if "\x00" in value:
            raise ValueError
        return len(json.dumps(value, ensure_ascii=False).encode("utf-8"))
    if value is None or type(value) in (bool, int):
        return len(json.dumps(value))
    if type(value) is float and math.isfinite(value):
        # This is JSON sizing for confidence/coordinates, never financial arithmetic.
        return len(format(Decimal(str(value)), "f"))
    raise ValueError


def canonical_json(value, limit, *, code="ocr_invalid_payload") -> str:
    try:
        if _json_size(value) > limit:
            raise ValueError
        return json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
        )
    except (ValueError, TypeError, UnicodeError, RecursionError, OverflowError):
        raise LedgerError(code, 422, "OCR data is invalid or exceeds its limits.") from None


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class InputModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)


class JobCreate(InputModel):
    intent_id: UUID
    file_id: UUID


class Candidate(InputModel):
    source_key: Annotated[StrictStr, Field(min_length=1, max_length=200)]
    recognized: dict[str, Any] = Field(repr=False)
    evidence: dict[str, Any] = Field(repr=False)
    fields: dict[str, Any] = Field(repr=False)

    @field_validator("source_key")
    @classmethod
    def stable_key(cls, value):
        if not value.strip() or any(unicodedata.category(char) == "Cc" for char in value):
            raise ValueError("A stable source key without control characters is required")
        return value


class Completion(InputModel):
    summary: dict[str, Any] = Field(default_factory=dict, repr=False)
    candidates: Annotated[tuple[Candidate, ...], Field(max_length=200)] = ()


@dataclass(frozen=True)
class Lease:
    owner_id: UUID
    ledger_id: UUID
    job_id: UUID
    file_id: UUID
    generation: int
    token: UUID = field(repr=False)
    lease_until: datetime
    configuration_json: str = field(repr=False)
    blob_key: str = field(repr=False)
    sha256: str
    byte_size: int
    media_type: str


class JobResult(BaseModel):
    summary: dict[str, Any]
    draft_ids: tuple[UUID, ...]


class JobView(BaseModel):
    id: UUID
    ledger_id: UUID
    file_id: UUID
    intent_id: UUID
    state: Literal["pending", "running", "succeeded", "failed"]
    attempts: int
    generation: int
    version: int
    retry_at: datetime
    error_code: str | None
    config_hash: str
    created_at: datetime
    updated_at: datetime
    result: JobResult | None


def prepare_configuration(value) -> tuple[dict, str]:
    code = "ocr_invalid_configuration"
    encoded = canonical_json(value, CONFIG_BYTES, code=code)
    value = json.loads(encoded)
    if (
        type(value) is not dict
        or set(value) != {"lease_seconds", "retry_seconds", "processing"}
        or type(value["lease_seconds"]) is not int
        or not 1 <= value["lease_seconds"] <= 3600
        or type(value["retry_seconds"]) is not int
        or not 0 <= value["retry_seconds"] <= 3600
        or type(value["processing"]) is not dict
    ):
        raise LedgerError(code, 422, "OCR configuration is invalid.")
    return json.loads(encoded), _hash(encoded)


def prepare_completion(completion: Completion) -> tuple[dict, str]:
    data = completion.model_dump(mode="python")
    data["candidates"] = list(data["candidates"])
    keys = [candidate["source_key"] for candidate in data["candidates"]]
    if len(set(keys)) != len(keys):
        raise LedgerError("ocr_invalid_payload", 422, "OCR source keys must be unique.")
    for candidate in data["candidates"]:
        for name in ("recognized", "evidence", "fields"):
            canonical_json(candidate[name], CANDIDATE_BYTES)
    encoded = canonical_json(data, RESULT_BYTES)
    # Reserve the exact envelope overhead before entering the completion transaction.
    canonical_json(
        {
            "format": 1,
            "fence_hash": "0" * 64,
            "payload_hash": "0" * 64,
            "summary": data["summary"],
            "draft_ids": ["0" * 36 for _ in keys],
        },
        RESULT_BYTES,
    )
    return json.loads(encoded), _hash(encoded)


def manifest_hash(owner_id: UUID, ledger_id: UUID, request: JobCreate) -> str:
    return _hash(
        canonical_json(
            {
                "format": 1,
                "owner_id": str(owner_id),
                "ledger_id": str(ledger_id),
                # Freeze format 1 independently of future DTO defaults.
                "request": {"intent_id": str(request.intent_id), "file_id": str(request.file_id)},
            },
            CONFIG_BYTES,
        )
    )


def fence_hash(lease: Lease) -> str:
    return _hash(
        canonical_json(
            {
                "job_id": str(lease.job_id),
                "owner_id": str(lease.owner_id),
                "ledger_id": str(lease.ledger_id),
                "file_id": str(lease.file_id),
                "generation": lease.generation,
                "token": str(lease.token),
            },
            CONFIG_BYTES,
        )
    )
