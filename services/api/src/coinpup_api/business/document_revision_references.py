"""Freeze the actual reviewed party, category and project facts for a corrected document."""

from copy import deepcopy
from dataclasses import dataclass
from uuid import UUID

from coinpup_api.business.confirmation_reads import BusinessConfirmedDocument
from coinpup_api.business.confirmations import confirmed_reference_rows
from coinpup_api.business.document_revision_schemas import ConfirmedDocumentReplacement
from coinpup_api.business.drafts import (
    CATEGORY_SNAPSHOT_FIELDS,
    PARTY_SNAPSHOT_FIELDS,
    PROJECT_SNAPSHOT_FIELDS,
    _snapshot,
)
from coinpup_api.ledger.service import LedgerError, _version

_FIELDS = {
    "party": PARTY_SNAPSHOT_FIELDS,
    "category": CATEGORY_SNAPSHOT_FIELDS,
    "project": PROJECT_SNAPSHOT_FIELDS,
}


@dataclass(frozen=True, slots=True)
class ReviewedLineReferences:
    source_line_id: UUID
    category_snapshot: dict
    project_snapshot: dict | None


@dataclass(frozen=True, slots=True)
class ReviewedDocumentReferences:
    issuer_snapshot: dict
    party_snapshot: dict
    lines: tuple[ReviewedLineReferences, ...]


def reviewed_document_references(
    session,
    ledger_id,
    current: BusinessConfirmedDocument,
    replacement: ConfirmedDocumentReplacement,
) -> ReviewedDocumentReferences:
    """Read each master collection once in the writer's locked transaction.

    Selected pins explicitly refresh that identity throughout the complete replacement.
    An unchanged unpinned reference retains the snapshot of that particular source line.
    """
    if current.ledger_id != ledger_id or current.document_kind != replacement.document_kind:
        raise LedgerError("document_revision_scope", 409, "The document scope is inconsistent.")
    party, categories, projects = confirmed_reference_rows(
        session, ledger_id, replacement, replacement.lines
    )
    pins = {(ref.kind, ref.id): ref.version for ref in replacement.reviewed_references}

    def resolve(kind, row, previous_id, saved):
        pin = pins.get((kind, row.id))
        if pin is not None:
            _version(row, pin)
            return _snapshot(row, _FIELDS[kind])
        if previous_id != row.id:
            raise LedgerError(
                "document_reference_review_required",
                409,
                "Review the new document reference before posting.",
            )
        if (
            not isinstance(saved, dict)
            or saved.get("id") != str(row.id)
            or type(saved.get("version")) is not int
            or not 1 <= saved["version"] <= row.version
        ):
            raise LedgerError(
                "document_revision_integrity", 503, "The stored reference snapshot is inconsistent."
            )
        return deepcopy(saved)

    saved_party = resolve("party", party, current.party_id, current.party_snapshot)
    old = {row.id: row for row in current.lines}
    lines = []
    for row in replacement.lines:
        previous = old.get(row.id)
        category = resolve(
            "category",
            categories[row.category_id],
            previous.category_id if previous else None,
            previous.category_snapshot if previous else None,
        )
        project = None
        if row.project_id is not None:
            project = resolve(
                "project",
                projects[row.project_id],
                previous.project_id if previous else None,
                previous.project_snapshot if previous else None,
            )
        lines.append(ReviewedLineReferences(row.id, category, project))
    return ReviewedDocumentReferences(deepcopy(current.issuer_snapshot), saved_party, tuple(lines))
