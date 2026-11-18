"""Rebuild an immutable document 200 and replay only its original ledger-scoped intent."""

from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from coinpup_api.business.document_revision_models import BusinessDocumentRevision
from coinpup_api.business.document_revision_receipt import (
    DocumentRevisionResponse,
    verify_document_receipt_sources,
)
from coinpup_api.business.document_revision_versions import read_confirmed_document_version
from coinpup_api.ledger.common import asset_definition
from coinpup_api.ledger.errors import MoneyError
from coinpup_api.ledger.idempotency import receipt_hash_version
from coinpup_api.ledger.models import AssetRecord
from coinpup_api.ledger.service import LedgerError


def _inconsistent():
    return LedgerError(
        "document_revision_integrity", 503, "Stored document revision evidence is inconsistent."
    )


def fixed_document_response(session, ledger_id, document_id, revision_id):
    try:
        result = DocumentRevisionResponse.model_validate(
            session.scalar(
                select(
                    func.coinpup_confirmed_document_revision_response(
                        ledger_id, document_id, revision_id
                    )
                )
            )
        )
        if (result.ledger_id, result.document_id, result.revision_id) != (
            ledger_id,
            document_id,
            revision_id,
        ):
            raise _inconsistent()
        groups = (
            (result.previous,)
            if result.replacement is None
            else (result.previous, result.replacement)
        )
        catalog = {
            row.asset_id: asset_definition(row)
            for row in session.scalars(
                select(AssetRecord).where(AssetRecord.asset_id.in_({g.asset_id for g in groups}))
            )
        }
        for group in groups:
            if group.asset_id not in catalog:
                raise _inconsistent()
            group.verify_prices(catalog[group.asset_id])
        return result
    except (MoneyError, ValidationError):
        raise _inconsistent() from None
    except IntegrityError as err:
        if (
            getattr(getattr(err.orig, "diag", None), "constraint_name", None)
            == "ck_document_revision_response"
        ):
            raise _inconsistent() from None
        raise


def document_receipt_anchor(response):
    # Every original or corrected active document has at least one positive accrual.
    return next(
        row.posting.operation_id for row in response.previous.lines if row.posting is not None
    )


def replay_document_revision(session, receipt, ledger_id, document_id, action, command, digest):
    """The caller authorizes and locks the ledger before checking the permanent key."""
    if receipt_hash_version(receipt) != 2 or receipt.request_hash != digest:
        raise LedgerError("idempotency_conflict", 409, "This key belongs to a different request.")
    if receipt.ledger_id != ledger_id or receipt.response_status != 200:
        raise _inconsistent()
    table = BusinessDocumentRevision
    header = session.scalar(
        select(table).where(
            table.ledger_id == ledger_id,
            table.document_id == document_id,
            table.idempotency_key == receipt.key,
        )
    )
    if (
        header is None
        or header.action != action
        or header.previous_version != command.expected_version
        or header.reason != command.reason
    ):
        raise _inconsistent()
    result = fixed_document_response(session, ledger_id, document_id, header.id)
    if receipt.response != result.model_dump(
        mode="json"
    ) or receipt.operation_id != document_receipt_anchor(result):
        raise _inconsistent()
    prior = read_confirmed_document_version(
        session, ledger_id, document_id, version=header.previous_version
    )
    if prior.document is None:
        raise _inconsistent()
    old_asset = asset_definition(session.get(AssetRecord, result.previous.asset_id))
    new_asset = (
        asset_definition(session.get(AssetRecord, result.replacement.asset_id))
        if result.replacement
        else None
    )
    try:
        verify_document_receipt_sources(
            result, prior.document, command, prior.postings, old_asset, new_asset
        )
    except (MoneyError, ValidationError):
        raise _inconsistent() from None
    return result
