"""Exact sequence text and owner-scoped change notification responses."""

import re
from datetime import UTC, datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import (
    AfterValidator,
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    StrictStr,
    field_validator,
)

MAX_CURSOR = (1 << 63) - 1
_CURSOR = re.compile(r"(?:0|[1-9][0-9]*)")
_NOTICE_TIMESTAMP = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}[Tt][0-9]{2}:[0-9]{2}:[0-9]{2}"
    r"(?:\.[0-9]+)?(?:[Zz]|[+-][0-9]{2}:[0-9]{2})"
)


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
    "ocr_confirmations",
    "business_parties",
    "business_documents",
    "business_document_lines",
    "ledger_periods",
    "ledger_period_audits",
    "ledger_period_receipts",
    "business_projects",
    "recurring_invoice_rules",
    "recurring_invoice_instances",
    "reminder_events",
    "reminder_event_revisions",
    "reminder_delivery_configs",
    "reminder_delivery_revisions",
    "business_confirmations",
    "business_postings",
    "business_payments",
    "business_allocations",
    "business_allocation_lines",
    "business_allocation_returns",
    "business_allocation_return_lines",
    "business_refunds",
    "business_refund_returns",
    "business_credits",
    "business_credit_lines",
    "business_credit_revisions",
    "business_credit_revision_members",
    "business_refund_revisions",
    "business_refund_revision_returns",
    "business_return_revisions",
    "business_return_revision_members",
    "business_document_revisions",
    "business_document_revision_lines",
    "business_document_accruals",
    "business_invoice_profiles",
    "business_invoice_reservations",
    "business_invoice_completions",
    "interledger_settings",
    "interledger_groups",
    "manual_prices",
    "reminder_email_deliveries",
    "reminder_email_evidence",
    "quote_observations",
    "reminder_inbox_deliveries",
    "platform_transfers",
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


def notice_utc_time(value: datetime) -> datetime:
    try:
        if value.utcoffset() is not None:
            return value.astimezone(UTC)
    except (OverflowError, ValueError):
        pass
    raise ValueError("Change evidence requires a zoned timestamp representable in UTC.")


class SavedNotice(ChangeRecord):
    model_config = ConfigDict(extra="forbid")
    changed_at: AwareDatetime

    @field_validator("changed_at", mode="before")
    @classmethod
    def timestamp_text(cls, value):
        # Internal callers may construct a witness from an already validated notice.
        if isinstance(value, datetime) or (
            isinstance(value, str) and _NOTICE_TIMESTAMP.fullmatch(value)
        ):
            return value
        raise ValueError("Use an RFC 3339 timestamp string with a time zone.")

    @field_validator("changed_at")
    @classmethod
    def representable_in_utc(cls, value):
        notice_utc_time(value)
        return value


class ChangeStart(BaseModel):
    cursor: Cursor
    witness: ChangeRecord | None


class DeviceRegistration(BaseModel):
    model_config = ConfigDict(extra="forbid")
    cursor: Cursor
    witness: SavedNotice | None
