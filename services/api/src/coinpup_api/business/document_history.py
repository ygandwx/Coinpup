"""Bounded confirmed-document history and independently verified permanent revision receipts."""

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictStr, ValidationError, model_validator
from sqlalchemy import Integer, String, Uuid, case, func, literal, select, union_all

from coinpup_api.business.confirmation_models import BusinessConfirmation as Confirmation
from coinpup_api.business.document_fixed_receipts import (
    _inconsistent,
    document_receipt_anchor,
    fixed_document_response,
)
from coinpup_api.business.document_revision_models import BusinessDocumentRevision as Revision
from coinpup_api.business.document_revision_receipt import DocumentRevisionResponse, StoredVersion
from coinpup_api.business.models import BusinessDocument
from coinpup_api.ledger.models import CommandReceipt
from coinpup_api.ledger.service import LedgerService, _not_found, _page


class DocumentHistoryEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    version: Annotated[StoredVersion, Field(ge=2)]
    revision_id: UUID | None
    action: Literal["confirm", "correct", "cancel"]
    reason: Annotated[StrictStr, Field(min_length=1, max_length=1000)] | None
    created_at: datetime
    previous_line_count: Annotated[int, Field(strict=True, ge=1, le=200)] | None
    replacement_line_count: Annotated[int, Field(strict=True, ge=0, le=200)] | None

    @model_validator(mode="after")
    def confirmation_or_revision(self):
        original = self.action == "confirm"
        if (self.revision_id is None) != original or (self.reason is None) != original:
            raise ValueError("Only original confirmation has no revision identity or reason")
        if original:
            if self.previous_line_count is not None or self.replacement_line_count is not None:
                raise ValueError("Original line count is carried by its sealed document")
        elif self.previous_line_count is None or self.replacement_line_count is None:
            raise ValueError("Revision metadata must retain both complete line counts")
        elif (self.replacement_line_count == 0) != (self.action == "cancel"):
            raise ValueError("Only cancellation has no replacement lines")
        return self


class BusinessDocumentHistory(LedgerService):
    def list_history(self, owner_id, ledger_id, document_id, *, limit=100, offset=0):
        _page(limit, offset)
        with self._transaction(owner_id, read_only=True) as session:
            self._ledger(session, owner_id, ledger_id)
            original = (
                select(
                    BusinessDocument.version.label("version"),
                    literal(None, type_=Uuid).label("revision_id"),
                    literal("confirm").label("action"),
                    literal(None, type_=String).label("reason"),
                    Confirmation.created_at,
                    literal(None, type_=Integer).label("previous_line_count"),
                    literal(None, type_=Integer).label("replacement_line_count"),
                )
                .join(
                    Confirmation,
                    (Confirmation.document_id == BusinessDocument.id)
                    & (Confirmation.ledger_id == BusinessDocument.ledger_id),
                )
                .where(
                    BusinessDocument.id == document_id,
                    BusinessDocument.ledger_id == ledger_id,
                    BusinessDocument.state == "confirmed",
                )
            )
            if session.execute(original).first() is None:
                raise _not_found()

            def line_count(group):
                value = CommandReceipt.response[group]["lines"]
                return case(
                    (func.jsonb_typeof(value) == "array", func.jsonb_array_length(value)),
                    else_=None,
                )

            directory = union_all(
                original,
                select(
                    Revision.document_version,
                    Revision.id,
                    Revision.action,
                    Revision.reason,
                    Revision.created_at,
                    line_count("previous"),
                    case((Revision.action == "cancel", 0), else_=line_count("replacement")),
                )
                .outerjoin(
                    CommandReceipt,
                    (CommandReceipt.ledger_id == Revision.ledger_id)
                    & (CommandReceipt.key == Revision.idempotency_key),
                )
                .where(Revision.document_id == document_id, Revision.ledger_id == ledger_id),
            ).subquery()
            rows = session.execute(
                select(directory).order_by(directory.c.version).limit(limit).offset(offset)
            ).mappings()
            try:
                return [DocumentHistoryEntry.model_validate(row) for row in rows]
            except ValidationError:
                raise _inconsistent() from None

    def get_revision(self, owner_id, ledger_id, document_id, revision_id):
        with self._transaction(owner_id, read_only=True) as session:
            self._ledger(session, owner_id, ledger_id)
            # Prove all supplied scope before invoking a function that reads sealed evidence.
            saved = session.scalar(
                select(Revision).where(
                    Revision.id == revision_id,
                    Revision.document_id == document_id,
                    Revision.ledger_id == ledger_id,
                )
            )
            if saved is None:
                raise _not_found()
            receipt = session.get(CommandReceipt, (ledger_id, saved.idempotency_key))
            if receipt is None:
                raise _inconsistent()
            try:
                DocumentRevisionResponse.model_validate(receipt.response)
            except ValidationError:
                raise _inconsistent() from None
            fixed = fixed_document_response(session, ledger_id, document_id, revision_id)
            if (
                receipt is None
                or receipt.hash_version != 2
                or receipt.response_status != 200
                or receipt.response != fixed.model_dump(mode="json")
                or receipt.operation_id != document_receipt_anchor(fixed)
                or fixed.confirmation_id != saved.confirmation_id
                or fixed.version != saved.document_version
                or fixed.previous_version != saved.previous_version
                or fixed.action != saved.action
                or fixed.reason != saved.reason
            ):
                raise _inconsistent()
            return fixed
