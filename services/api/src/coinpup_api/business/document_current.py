"""Authorized current confirmed facts; original confirmation remains a separate fixed read."""

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from coinpup_api.business.confirmation_reads import BusinessConfirmedDocument
from coinpup_api.business.document_revision_receipt import DocumentAccrualPosting, StoredVersion
from coinpup_api.business.document_revision_versions import read_confirmed_document_version
from coinpup_api.ledger.service import LedgerError, LedgerService


class DocumentCurrentPosting(DocumentAccrualPosting):
    source_line_id: UUID


class DocumentCurrent(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    ledger_id: UUID
    document_id: UUID
    confirmation_id: UUID
    document_kind: Literal["invoice", "bill"]
    version: StoredVersion
    status: Literal["active", "cancelled"]
    revision_id: UUID | None
    document: BusinessConfirmedDocument | None
    postings: list[DocumentCurrentPosting] = Field(max_length=200)

    @model_validator(mode="after")
    def complete_current_facts(self):
        if self.status == "cancelled":
            if self.document is not None or self.postings or self.revision_id is None:
                raise ValueError("Cancelled documents have no current accruals")
            return self
        d = self.document
        if d is None or (d.id, d.ledger_id, d.confirmation_id, d.document_kind, d.version) != (
            self.document_id,
            self.ledger_id,
            self.confirmation_id,
            self.document_kind,
            self.version,
        ):
            raise ValueError("Current facts must identify the exact confirmed document version")
        positive = [r.id for r in d.lines if any(c in "123456789" for c in r.total_amount)]
        if [p.source_line_id for p in self.postings] != positive or not positive:
            raise ValueError("Every positive current source needs exactly one ordered accrual")
        if len({p.operation_id for p in self.postings}) != len(self.postings):
            raise ValueError("Current accrual identities are distinct")
        return self


class BusinessDocumentCurrentReads(LedgerService):
    def current(self, owner_id, ledger_id, document_id):
        with self._transaction(owner_id, read_only=True) as session:
            self._ledger(session, owner_id, ledger_id)
            facts = read_confirmed_document_version(session, ledger_id, document_id)
            original = facts.original.document
            try:
                return DocumentCurrent(
                    ledger_id=ledger_id,
                    document_id=document_id,
                    confirmation_id=original.confirmation_id,
                    document_kind=original.document_kind,
                    version=facts.version,
                    status="cancelled" if facts.document is None else "active",
                    revision_id=facts.revision_id,
                    document=facts.document,
                    postings=[
                        DocumentCurrentPosting(
                            source_line_id=p.source_line_id,
                            operation_id=p.operation_id,
                            operation_version=p.version,
                            journal_id=p.journal_id,
                        )
                        for p in facts.postings
                    ],
                )
            except ValidationError:
                raise LedgerError(
                    "document_revision_integrity", 503, "Stored document facts are inconsistent."
                ) from None
