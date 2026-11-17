"""Plan a complete confirmed document transition, including lines with zero accrual."""

from dataclasses import dataclass, replace
from datetime import date
from decimal import Decimal
from typing import Literal
from uuid import UUID

from coinpup_api.business.confirmation_plan import AccrualLinePlan, accrual_line
from coinpup_api.business.confirmation_reads import BusinessConfirmedDocument
from coinpup_api.business.document_revision_schemas import ConfirmedDocumentReplacement
from coinpup_api.business.draft_schemas import BusinessDraftLineInput, BusinessDraftLineResponse
from coinpup_api.business.pricing import price_document_rows
from coinpup_api.ledger.assets import AssetDefinition
from coinpup_api.ledger.errors import MoneyError
from coinpup_api.ledger.money import Amount


@dataclass(frozen=True, slots=True)
class SealedAccrual:
    ledger_id: UUID
    document_id: UUID
    source_line_id: UUID
    operation_id: UUID
    version: int
    journal_id: UUID


@dataclass(frozen=True, slots=True)
class DocumentLineTransition:
    source_line_id: UUID
    previous: BusinessDraftLineResponse | None
    previous_posting: SealedAccrual | None
    line_no: int | None
    replacement: BusinessDraftLineInput | None
    accrual: AccrualLinePlan | None

    @property
    def posting_action(self) -> Literal["create", "correct", "cancel"] | None:
        if self.previous_posting is None:
            return "create" if self.accrual is not None else None
        return "correct" if self.accrual is not None else "cancel"

    @property
    def next_operation_version(self) -> int | None:
        if self.previous_posting is not None:
            return self.previous_posting.version + 1
        return 1 if self.accrual is not None else None


@dataclass(frozen=True, slots=True)
class DocumentAccrualTransition:
    previous_version: int
    version: int
    lines: tuple[DocumentLineTransition, ...]
    affected_dates: tuple[date, ...]


def _inconsistent():
    return MoneyError(
        "document_revision_integrity", "The current confirmed document is inconsistent."
    )


def _sealed_postings(document, old_asset, postings):
    rows = document.lines
    if (
        document.state != "confirmed"
        or document.document_kind not in {"invoice", "bill"}
        or document.asset_id != old_asset.asset_id
        or not 1 <= len(rows) <= 200
        or not 2 <= document.version < 2147483647
        or len(rows) != document.line_count
        or len({row.id for row in rows}) != len(rows)
        or [row.line_no for row in rows] != sorted({row.line_no for row in rows})
        or any(
            row.ledger_id != document.ledger_id
            or row.document_id != document.id
            or row.archived
            or row.asset_id != document.asset_id
            or type(row.line_no) is not int
            or row.line_no < 1
            or row.category_kind != ("income" if document.document_kind == "invoice" else "expense")
            for row in rows
        )
    ):
        raise _inconsistent()
    # Decode the previous version using its original precision even when disabled for new postings.
    priced = price_document_rows(rows, replace(old_asset, enabled=True))
    for source, price in ((document, priced), *zip(rows, priced.lines, strict=True)):
        if any(
            Amount.from_decimal(Decimal(getattr(source, name + "_amount")), old_asset)
            != getattr(price, name)
            for name in ("net", "tax", "total")
        ):
            raise _inconsistent()
    positive = {
        row.id for row, price in zip(rows, priced.lines, strict=True) if price.total.minor_units
    }
    found = {p.source_line_id: p for p in postings}
    if (
        not positive
        or found.keys() != positive
        or len(found) != len(postings)
        or len({p.operation_id for p in postings}) != len(postings)
        or len({p.journal_id for p in postings}) != len(postings)
        or any(
            p.ledger_id != document.ledger_id
            or p.document_id != document.id
            or not isinstance(p.operation_id, UUID)
            or not isinstance(p.journal_id, UUID)
            or type(p.version) is not int
            or not 1 <= p.version < document.version
            for p in postings
        )
    ):
        raise _inconsistent()
    return found


def plan_document_accrual_revision(
    current: BusinessConfirmedDocument,
    old_asset: AssetDefinition,
    postings: tuple[SealedAccrual, ...],
    replacement: ConfirmedDocumentReplacement | None,
    new_asset: AssetDefinition | None,
) -> DocumentAccrualTransition:
    """Link only active positive lines to the operations that a write must reverse.

    Old positions may have gaps after draft edits. New positions follow the reviewed
    replacement order. Zero and retired lines retain facts without receiving a zero journal.
    The caller must lock the ledger, check the expected version, all dates and capacities,
    then reverse the actual immutable journals. This planner allocates no persistence IDs.
    """
    active = _sealed_postings(current, old_asset, postings)
    previous = {row.id: row for row in current.lines}
    days = {current.issue_date, *(row.recognition_date for row in current.lines)}
    if (replacement is None) != (new_asset is None):
        raise _inconsistent()
    lines = []
    if replacement is not None:
        prices = replacement.price(current.document_kind, new_asset)
        days.add(replacement.issue_date)
        for position, (row, price) in enumerate(
            zip(replacement.lines, prices.lines, strict=True), 1
        ):
            days.add(row.recognition_date)
            lines.append(
                DocumentLineTransition(
                    row.id,
                    previous.pop(row.id, None),
                    active.get(row.id),
                    position,
                    row,
                    accrual_line(row, position, price, current.document_kind),
                )
            )
    lines.extend(
        DocumentLineTransition(row.id, row, active.get(row.id), None, None, None)
        for row in previous.values()
    )
    return DocumentAccrualTransition(
        current.version, current.version + 1, tuple(lines), tuple(sorted(days))
    )
