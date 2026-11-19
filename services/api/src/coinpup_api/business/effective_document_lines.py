"""Read the effective confirmed source facts for one document or an explicit list of lines."""

from sqlalchemy import Date, Integer, Numeric, String, Uuid, cast, column, func, select, true
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Bundle

from coinpup_api.business.document_revision_models import BusinessDocumentRevision
from coinpup_api.business.models import BusinessDocument
from coinpup_api.ledger.models import AssetRecord
from coinpup_api.ledger.service import _not_found


def effective_document_header(session, ledger_id, document_id):
    """Read current identity and scale within the caller's owned snapshot in one query.

    A terminal document has no new priced fields. Its empty capacity retains the last
    reviewed asset, party and issue date while advancing to the cancellation version.
    """
    doc, revision = BusinessDocument, BusinessDocumentRevision
    owned = (revision.ledger_id == doc.ledger_id, revision.document_id == doc.id)
    priced = (
        select(revision.party_id, revision.asset_id, revision.issue_date)
        .where(*owned, revision.action == "correct")
        .order_by(revision.document_version.desc())
        .limit(1)
        .correlate(doc)
        .lateral()
    )
    latest = (
        select(revision.document_version, revision.action)
        .where(*owned)
        .order_by(revision.document_version.desc())
        .limit(1)
        .correlate(doc)
        .lateral()
    )
    asset = func.coalesce(priced.c.asset_id, doc.asset_id)
    current = Bundle(
        "document",
        doc.id,
        doc.ledger_id,
        doc.document_kind,
        func.coalesce(priced.c.party_id, doc.party_id).label("party_id"),
        asset.label("asset_id"),
        func.coalesce(priced.c.issue_date, doc.issue_date).label("issue_date"),
        func.coalesce(latest.c.document_version, doc.version).label("version"),
    )
    result = session.execute(
        select(current, AssetRecord, func.coalesce(latest.c.action == "cancel", False))
        .select_from(doc)
        .outerjoin(priced, true())
        .outerjoin(latest, true())
        .join(AssetRecord, AssetRecord.asset_id == asset)
        .where(doc.id == document_id, doc.ledger_id == ledger_id, doc.state == "confirmed")
    ).one_or_none()
    if result is None:
        raise _not_found()
    return result


def effective_document_lines(ledger_id, *, document_id=None, line_ids=None):
    return func.public.coinpup_effective_document_lines(
        cast(ledger_id, Uuid),
        cast(document_id, Uuid),
        cast(None if line_ids is None else list(line_ids), ARRAY(Uuid)),
    ).table_valued(
        column("ledger_id", Uuid),
        column("confirmation_id", Uuid),
        column("document_id", Uuid),
        column("document_version", Integer),
        column("document_kind", String),
        column("party_id", Uuid),
        column("asset_id", String),
        column("issue_date", Date),
        column("source_line_id", Uuid),
        column("line_no", Integer),
        column("description", String),
        column("category_id", Uuid),
        column("category_kind", String),
        column("project_id", Uuid),
        column("recognition_date", Date),
        column("net_amount", Numeric),
        column("tax_amount", Numeric),
        column("total_amount", Numeric),
        column("operation_id", Uuid),
        column("operation_version", Integer),
        column("journal_id", Uuid),
    )
