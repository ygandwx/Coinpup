"""Exact sequence text and owner-scoped change notification responses."""

import re
from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import AfterValidator, BaseModel, Field, StrictInt, StrictStr

MAX_CURSOR = (1 << 63) - 1
_CURSOR = re.compile(r"(?:0|[1-9][0-9]*)")


def cursor_value(value) -> int:
    """Validate before conversion; cursors never accept coercion or unbounded integers."""
    if not isinstance(value, str) or not 1 <= len(value) <= 19 or _CURSOR.fullmatch(value) is None:
        raise ValueError("Use a bounded canonical decimal cursor")
    parsed = int(value)
    if parsed > MAX_CURSOR:
        raise ValueError("Use a bounded canonical decimal cursor")
    return parsed


def _cursor_text(value: str) -> str:
    cursor_value(value)
    return value


Cursor = Annotated[
    StrictStr,
    Field(min_length=1, max_length=19, pattern=r"^(0|[1-9][0-9]*)$"),
    AfterValidator(_cursor_text),
]
SequenceText = Annotated[Cursor, Field(pattern=r"^[1-9][0-9]*$")]
EntityType = Literal[
    "entities",
    "ledgers",
    "accounts",
    "account_assets",
    "categories",
    "assets",
    "financial_operations",
    "stored_files",
    "operation_file_links",
    "ocr_jobs",
    "ocr_drafts",
]
ChangeKind = Literal["upsert", "archive", "restore", "cancel"]


class ChangeRecord(BaseModel):
    seq: SequenceText
    owner_id: UUID
    ledger_id: UUID | None
    entity_type: EntityType
    entity_id: Annotated[StrictStr, Field(min_length=1)]
    entity_version: Annotated[StrictInt, Field(ge=1)]
    change_kind: ChangeKind
    changed_at: datetime


class ChangePage(BaseModel):
    changes: list[ChangeRecord]
    next_cursor: Cursor
