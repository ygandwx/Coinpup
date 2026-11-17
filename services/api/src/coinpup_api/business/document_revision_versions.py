"""Read one exact confirmed-document version using frozen sources in a caller-owned snapshot."""

from dataclasses import dataclass, replace
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy import select

from coinpup_api.business.confirmation_reads import (
    BusinessConfirmationDetail,
    BusinessConfirmedDocument,
    original_confirmation_detail,
)
from coinpup_api.business.document_revision_models import (
    BusinessDocumentAccrual,
    BusinessDocumentRevision,
    BusinessDocumentRevisionLine,
)
from coinpup_api.business.document_revision_plan import SealedAccrual
from coinpup_api.business.draft_schemas import BusinessDraftLineInput, BusinessDraftLineResponse
from coinpup_api.business.drafts import AMOUNTS, _amounts
from coinpup_api.business.pricing import MAX_DOCUMENT_LINES, price_document_rows
from coinpup_api.ledger.common import asset_definition
from coinpup_api.ledger.errors import MoneyError
from coinpup_api.ledger.models import AssetRecord
from coinpup_api.ledger.money import Amount
from coinpup_api.ledger.service import LedgerError, _not_found


@dataclass(frozen=True, slots=True)
class ConfirmedDocumentVersion:
    original: BusinessConfirmationDetail
    revision_id: UUID | None
    version: int
    document: BusinessConfirmedDocument | None
    postings: tuple[SealedAccrual, ...]


def _integrity():
    return LedgerError(
        "document_revision_integrity", 503, "Stored document facts are inconsistent."
    )


def _original(detail):
    doc = detail.document
    postings = tuple(
        SealedAccrual(doc.ledger_id, doc.id, p.line_id, p.operation_id, 1, p.journal_id)
        for p in detail.confirmation.postings
    )
    return ConfirmedDocumentVersion(detail, None, doc.version, doc, postings)


def _headers():
    return select(BusinessDocumentRevision, AssetRecord).outerjoin(
        AssetRecord, AssetRecord.asset_id == BusinessDocumentRevision.asset_id
    )


def _active(session, original, header, catalog):
    doc = original.document
    fact, link = BusinessDocumentRevisionLine, BusinessDocumentAccrual
    facts = session.scalars(
        select(fact)
        .where(
            fact.revision_id == header.id,
            fact.document_id == doc.id,
            fact.ledger_id == doc.ledger_id,
            fact.confirmation_id == doc.confirmation_id,
            fact.document_version == header.document_version,
        )
        .order_by(fact.line_no)
        .limit(MAX_DOCUMENT_LINES + 1)
    ).all()
    links = session.scalars(
        select(link)
        .where(
            link.revision_id == header.id,
            link.document_id == doc.id,
            link.ledger_id == doc.ledger_id,
            link.confirmation_id == doc.confirmation_id,
            link.document_version == header.document_version,
        )
        .limit(2 * MAX_DOCUMENT_LINES + 1)
    ).all()
    if not 1 <= len(links) <= 2 * MAX_DOCUMENT_LINES:
        raise _integrity()
    if header.action == "cancel":
        if facts or any(link.action != "cancel" for link in links):
            raise _integrity()
        return ConfirmedDocumentVersion(
            original, header.id, header.document_version, None, ()
        ), links
    if not 1 <= len(facts) <= MAX_DOCUMENT_LINES or [f.line_no for f in facts] != list(
        range(1, len(facts) + 1)
    ):
        raise _integrity()
    if catalog is None:
        raise _integrity()
    asset = replace(asset_definition(catalog), enabled=True)
    prices = price_document_rows(facts, asset)
    if prices.total.minor_units <= 0:
        raise _integrity()
    by_fact = {
        link.replacement_fact_id: link for link in links if link.replacement_fact_id is not None
    }
    lines = []
    postings = []
    for f, price in zip(facts, prices.lines, strict=True):
        posting = by_fact.pop(f.id, None)
        if any(
            Amount.from_decimal(getattr(f, name + "_amount"), asset) != getattr(price, name)
            for name in AMOUNTS
        ):
            raise _integrity()
        if bool(price.total.minor_units) != (posting is not None):
            raise _integrity()
        if posting is not None:
            if posting.source_line_id != f.source_line_id or posting.journal_id is None:
                raise _integrity()
            postings.append(
                SealedAccrual(
                    doc.ledger_id,
                    doc.id,
                    f.source_line_id,
                    posting.operation_id,
                    posting.operation_version,
                    posting.journal_id,
                )
            )
        source = {
            name: getattr(f, name) for name in BusinessDraftLineInput.model_fields if name != "id"
        }
        lines.append(
            BusinessDraftLineResponse(
                **source,
                id=f.source_line_id,
                document_id=doc.id,
                ledger_id=doc.ledger_id,
                line_no=f.line_no,
                asset_id=asset.asset_id,
                category_kind=f.category_kind,
                archived=False,
                category_snapshot=f.category_snapshot,
                project_snapshot=f.project_snapshot,
                version=f.version,
                created_at=f.created_at,
                updated_at=f.created_at,
                **_amounts(price),
            )
        )
    if by_fact:
        raise _integrity()
    effective = BusinessConfirmedDocument.model_validate(
        doc.model_dump()
        | dict(
            version=header.document_version,
            updated_at=header.created_at,
            party_id=header.party_id,
            asset_id=header.asset_id,
            issue_date=header.issue_date,
            due_date=header.due_date,
            notes=header.notes,
            party_snapshot=header.party_snapshot,
            lines=lines,
            line_count=len(lines),
            **_amounts(prices),
        )
    )
    return ConfirmedDocumentVersion(
        original, header.id, header.document_version, effective, tuple(postings)
    ), links


def _prove_previous(session, original, header, links):
    if header.previous_revision_id is None:
        previous = _original(original)
        if header.previous_version != previous.version:
            raise _integrity()
        postings = previous.postings
    else:
        parent = session.execute(
            _headers().where(
                BusinessDocumentRevision.id == header.previous_revision_id,
                BusinessDocumentRevision.ledger_id == header.ledger_id,
                BusinessDocumentRevision.document_id == header.document_id,
                BusinessDocumentRevision.confirmation_id == header.confirmation_id,
                BusinessDocumentRevision.document_version == header.previous_version,
            )
        ).one_or_none()
        if parent is None or parent[0].action != "correct":
            raise _integrity()
        previous, _ = _active(session, original, *parent)
        postings = previous.postings
    old = {p.source_line_id: p for p in postings}
    for link in links:
        before = old.pop(link.source_line_id, None)
        if before is None:
            if link.action != "create":
                raise _integrity()
        elif (
            link.action not in ("correct", "cancel")
            or link.previous_journal_id != before.journal_id
            or link.operation_id != before.operation_id
            or link.operation_version != before.version + 1
        ):
            raise _integrity()
    if old:
        raise _integrity()


def read_confirmed_document_version(session, ledger_id, document_id, *, version=None):
    """Read the latest or an exact version, including terminal cancellation."""
    with session.no_autoflush:
        detail = original_confirmation_detail(session, ledger_id, document_id)
        first = detail.document.version
        if version is not None:
            if type(version) is not int or not first <= version <= 2147483647:
                raise _not_found()
            if version == first:
                return _original(detail)
        table = BusinessDocumentRevision
        query = _headers().where(
            table.ledger_id == ledger_id,
            table.document_id == document_id,
            table.confirmation_id == detail.confirmation.id,
        )
        if version is not None:
            query = query.where(table.document_version == version)
        saved = session.execute(
            query.order_by(table.document_version.desc()).limit(1)
        ).one_or_none()
        if saved is None:
            if version is not None:
                raise _not_found()
            return _original(detail)
        try:
            header, catalog = saved
            result, links = _active(session, detail, header, catalog)
            _prove_previous(session, detail, header, links)
        except (MoneyError, ValidationError):
            raise _integrity() from None
        return result
