"""List issuance numbers without loading original snapshots, receipts, or private files."""

from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr, model_validator
from sqlalchemy import func, select, true

from coinpup_api.business.document_revision_models import BusinessDocumentRevision
from coinpup_api.business.invoice_completion_models import InvoiceCompletion
from coinpup_api.business.invoice_numbering import MAX_SEQUENCE, SEQUENCE
from coinpup_api.business.invoice_reservation_models import InvoiceReservation
from coinpup_api.business.models import BusinessDocument
from coinpup_api.ledger.service import LedgerError, LedgerService

Cursor = Annotated[StrictStr, Field(pattern=r"^[1-9][0-9]{0,18}$", max_length=19)]
Version = Annotated[StrictInt, Field(ge=2, le=2147483647)]


class InvoiceHistoryItem(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    id: UUID
    ledger_id: UUID
    document_id: UUID
    document_version: Version
    previous_issuance_id: UUID | None
    sequence: Cursor
    number: Annotated[StrictStr, Field(min_length=1, max_length=60)]
    state: Literal["pending", "complete"]
    current_document_version: Version
    document_status: Literal["current", "superseded", "cancelled"]

    @model_validator(mode="after")
    def coherent(self):
        if (
            int(self.sequence) > MAX_SEQUENCE
            or self.previous_issuance_id == self.id
            or self.document_version > self.current_document_version
        ):
            raise ValueError("Invalid stored Invoice history")
        if (self.document_version == self.current_document_version) != (
            self.document_status == "current"
        ):
            raise ValueError("Invoice history does not match the current document version")
        return self


class InvoiceHistoryPage(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    items: tuple[InvoiceHistoryItem, ...] = Field(max_length=200)
    next_before: Cursor | None


def _invalid():
    return LedgerError("invoice_issuance_integrity", 503, "Stored Invoice history is inconsistent.")


class InvoiceHistoryReads(LedgerService):
    def list(self, owner, ledger, *, before=None, document_id=None, limit=50):
        if (
            type(limit) is not int
            or not 1 <= limit <= 200
            or (
                before is not None
                and (
                    type(before) is not str
                    or not SEQUENCE.fullmatch(before)
                    or int(before) > MAX_SEQUENCE
                )
            )
        ):
            raise LedgerError(
                "invalid_request", 422, "Use a valid Invoice history cursor and page size."
            )
        with self._transaction(owner, read_only=True) as session:
            _, entity = self._ledger(session, owner, ledger)
            if entity.kind != "company":
                raise LedgerError(
                    "invoice_requires_company", 422, "Invoice history requires a company."
                )
            r, c, d, v = (
                InvoiceReservation,
                InvoiceCompletion,
                BusinessDocument,
                BusinessDocumentRevision,
            )
            # Bound the set of candidate reservations before joining or looking up latest versions.
            page = select(
                r.id,
                r.ledger_id,
                r.created_by,
                r.document_id,
                r.document_version,
                r.previous_issuance_id,
                r.sequence,
                r.number,
            ).where(r.ledger_id == ledger, r.created_by == owner)
            if before is not None:
                page = page.where(r.sequence < int(before))
            if document_id is not None:
                page = page.where(r.document_id == document_id)
            page = page.order_by(r.sequence.desc()).limit(limit + 1).subquery()
            # Revisions have a scoped unique index by document and monotonic document version.
            # A single lateral lookup returns both fields of the exact same latest row.
            latest = (
                select(v.document_version, v.action)
                .where(v.ledger_id == page.c.ledger_id, v.document_id == page.c.document_id)
                .order_by(v.document_version.desc())
                .limit(1)
                .lateral()
            )
            query = (
                select(
                    page.c.id,
                    page.c.ledger_id,
                    page.c.document_id,
                    page.c.document_version,
                    page.c.previous_issuance_id,
                    page.c.sequence,
                    page.c.number,
                    c.id.label("completion"),
                    c.ledger_id.label("completed_ledger"),
                    c.created_by.label("completed_owner"),
                    c.response_status,
                    d.document_kind,
                    func.coalesce(latest.c.document_version, d.version).label("current_version"),
                    latest.c.action,
                )
                .select_from(page)
                .outerjoin(c, c.id == page.c.id)
                .outerjoin(d, (d.id == page.c.document_id) & (d.ledger_id == page.c.ledger_id))
                .outerjoin(latest, true())
                .order_by(page.c.sequence.desc())
            )
            rows = session.execute(query).all()
            items = []
            for row in rows[:limit]:
                if (
                    row.document_kind != "invoice"
                    or row.action not in (None, "correct", "cancel")
                    or (
                        row.completion is not None
                        and (row.completed_ledger, row.completed_owner, row.response_status)
                        != (ledger, owner, 201)
                    )
                ):
                    raise _invalid()
                try:
                    items.append(
                        InvoiceHistoryItem(
                            id=row.id,
                            ledger_id=ledger,
                            document_id=row.document_id,
                            document_version=row.document_version,
                            previous_issuance_id=row.previous_issuance_id,
                            sequence=str(row.sequence),
                            number=row.number,
                            state="pending" if row.completion is None else "complete",
                            current_document_version=row.current_version,
                            document_status="cancelled"
                            if row.action == "cancel"
                            else "current"
                            if row.document_version == row.current_version
                            else "superseded",
                        )
                    )
                except (ValueError, TypeError):
                    raise _invalid() from None
            return InvoiceHistoryPage(
                items=tuple(items), next_before=items[-1].sequence if len(rows) > limit else None
            )
