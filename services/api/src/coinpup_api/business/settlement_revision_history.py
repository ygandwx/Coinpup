"""Bounded version directories and scoped permanent receipts for immutable settlements."""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    StrictStr,
    ValidationError,
    model_validator,
)
from sqlalchemy import select
from sqlalchemy.orm import InstrumentedAttribute

from coinpup_api.ledger.models import CommandReceipt
from coinpup_api.ledger.service import _not_found


class SettlementHistoryEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    version: Annotated[StrictInt, Field(ge=1, le=2147483647)]
    revision_id: UUID | None
    action: Literal["create", "correct", "cancel"]
    reason: Annotated[StrictStr, Field(min_length=1, max_length=2000)]
    created_at: datetime

    @model_validator(mode="after")
    def original_or_revision(self):
        if (self.action == "create") != (self.version == 1) or (self.revision_id is None) != (
            self.version == 1
        ):
            raise ValueError("Only the original creation has no revision identifier")
        return self


@dataclass(frozen=True, slots=True)
class SettlementHistory:
    """Select one immutable model pair and its domain-specific receipt verifier."""

    original: type
    revision: type
    parent_id: InstrumentedAttribute
    version_column: InstrumentedAttribute
    entry_type: type[SettlementHistoryEntry]
    integrity: Callable
    fixed_receipt: Callable

    def page(self, session, ledger_id, identifier, *, limit, offset):
        original, revision = self.original, self.revision
        origin = (
            session.execute(
                select(original.version, original.reason, original.created_at).where(
                    original.id == identifier, original.ledger_id == ledger_id
                )
            )
            .mappings()
            .one_or_none()
        )
        if origin is None:
            raise _not_found()
        entries = []
        try:
            if offset == 0:
                entries.append(self.entry_type(**origin, revision_id=None, action="create"))
            if len(entries) < limit:
                start = max(2, offset + 1)
                rows = session.execute(
                    select(
                        self.version_column.label("version"),
                        revision.id.label("revision_id"),
                        revision.action,
                        revision.revision_reason.label("reason"),
                        revision.created_at,
                    )
                    .where(
                        revision.ledger_id == ledger_id,
                        self.parent_id == identifier,
                        self.version_column >= start,
                    )
                    .order_by(self.version_column)
                    .limit(limit - len(entries))
                ).mappings()
                for expected, row in enumerate(rows, start):
                    if row["version"] != expected:
                        raise self.integrity()
                    entries.append(self.entry_type.model_validate(row))
            return entries
        except ValidationError:
            raise self.integrity() from None

    def receipt(self, session, ledger_id, identifier, revision_id):
        revision = self.revision
        row = session.execute(
            select(revision, CommandReceipt)
            .outerjoin(
                CommandReceipt,
                (CommandReceipt.ledger_id == revision.ledger_id)
                & (CommandReceipt.key == revision.idempotency_key),
            )
            .where(
                revision.id == revision_id,
                self.parent_id == identifier,
                revision.ledger_id == ledger_id,
            )
        ).one_or_none()
        if row is None:
            raise _not_found()
        return self.fixed_receipt(session, *row)[0]
