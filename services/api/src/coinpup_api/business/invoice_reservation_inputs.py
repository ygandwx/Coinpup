"""Build a new, transient reservation inside the caller's locked company transaction."""

import json

from sqlalchemy import func, select

from coinpup_api.business.document_current import read_current_document
from coinpup_api.business.invoice_issuance_hash import (
    invoice_issuance_hash_v2,
    invoice_issuance_payload,
)
from coinpup_api.business.invoice_logo import MAX_LOGO_BYTES
from coinpup_api.business.invoice_numbering import MAX_SEQUENCE, candidate_number
from coinpup_api.business.invoice_profile_models import InvoiceProfile
from coinpup_api.business.invoice_profiles import profile_response
from coinpup_api.business.invoice_render_snapshot import (
    INVOICE_TEMPLATE_VERSION,
    freeze_invoice_render_input,
)
from coinpup_api.business.invoice_reservation_models import InvoiceReservation
from coinpup_api.files.models import StoredFile
from coinpup_api.files.schemas import FileResponse
from coinpup_api.ledger.common import asset_definition
from coinpup_api.ledger.errors import MoneyError
from coinpup_api.ledger.idempotency import _IDEMPOTENCY_KEY
from coinpup_api.ledger.models import AssetRecord
from coinpup_api.ledger.service import LedgerError, _not_found, _version


def _next_number(session, ledger, policy):
    last = session.scalar(
        select(func.max(InvoiceReservation.sequence)).where(InvoiceReservation.ledger_id == ledger)
    )
    if last == MAX_SEQUENCE:
        raise LedgerError("invoice_sequence_exhausted", 409, "The Invoice sequence is exhausted.")
    sequence = (last or 0) + 1
    number = candidate_number(policy, str(sequence)).number
    if (
        session.scalar(
            select(InvoiceReservation.id).where(
                InvoiceReservation.ledger_id == ledger, InvoiceReservation.number == number
            )
        )
        is not None
    ):
        raise LedgerError(
            "invoice_number_conflict", 409, "The configuration would reuse an existing number."
        )
    return sequence, number


def new_reservation_input(session, *, owner, ledger, command, key, raw_body=None):
    """Only for a new intent after authorization, key lookup and the ordered write locks.

    The caller owns the transaction. Do not use this path for replay, resuming a
    frozen reservation or predicting a number outside the write lock.
    """
    if type(key) is not str or not _IDEMPOTENCY_KEY.fullmatch(key):
        raise LedgerError("invalid_idempotency_key", 422, "Use a printable ASCII command key.")
    original = invoice_issuance_payload(command, raw_body=raw_body)
    request = json.dumps(original, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    if len(request.encode("utf-8")) > 65536:
        raise LedgerError("invalid_request", 422, "The reviewed Invoice request is too large.")

    current = read_current_document(session, ledger, command.document_id)
    _version(current, command.expected_document_version)
    if current.document_kind != "invoice" or current.status != "active":
        raise LedgerError("invoice_not_issuable", 409, "Choose an active confirmed Invoice.")
    profile = session.scalar(
        select(InvoiceProfile).where(
            InvoiceProfile.id == command.configuration_id, InvoiceProfile.ledger_id == ledger
        )
    )
    if profile is None:
        raise _not_found()
    _version(profile, command.expected_configuration_version)
    if profile.archived:
        raise LedgerError(
            "invoice_profile_archived", 409, "Choose an active Invoice configuration."
        )
    config = profile_response(profile)
    file = None
    if config.logo_file_id is not None:
        file = session.scalar(
            select(StoredFile).where(
                StoredFile.id == config.logo_file_id,
                StoredFile.ledger_id == ledger,
                StoredFile.created_by == owner,
            )
        )
        if file is None or file.archived or file.version < config.logo_file_version:
            raise LedgerError("invoice_logo_unavailable", 409, "Review the selected Invoice Logo.")
        if file.byte_size > MAX_LOGO_BYTES:
            raise LedgerError(
                "invoice_logo_size_limit",
                422,
                "Choose an Invoice Logo within the rendering size limit.",
            )
    catalog = session.get(AssetRecord, current.document.asset_id)
    if catalog is None:
        raise LedgerError(
            "invoice_reservation_integrity", 503, "The saved Invoice unit is unavailable."
        )
    prev = session.execute(
        select(InvoiceReservation.id, InvoiceReservation.document_version)
        .where(
            InvoiceReservation.ledger_id == ledger,
            InvoiceReservation.document_id == current.document_id,
        )
        .order_by(InvoiceReservation.sequence.desc())
        .limit(1)
    ).one_or_none()
    if command.previous_issuance_id != (prev.id if prev else None) or (
        prev and prev.document_version >= current.version
    ):
        raise LedgerError(
            "invoice_predecessor_conflict", 409, "Review the latest issuance and document version."
        )
    sequence, number = _next_number(session, ledger, config.numbering)
    try:
        frozen = freeze_invoice_render_input(
            current,
            asset_definition(catalog),
            config,
            ledger_id=ledger,
            command=command,
            sequence=str(sequence),
            logo=FileResponse.model_validate(file) if file else None,
        )
    except MoneyError:
        raise LedgerError(
            "invoice_reservation_integrity", 503, "The reviewed Invoice facts are inconsistent."
        ) from None
    return InvoiceReservation(
        id=command.id,
        created_by=owner,
        ledger_id=ledger,
        version=1,
        document_id=command.document_id,
        document_version=current.version,
        configuration_id=config.id,
        configuration_version=config.version,
        previous_issuance_id=command.previous_issuance_id,
        logo_file_id=config.logo_file_id,
        sequence=sequence,
        number=number,
        template_version=INVOICE_TEMPLATE_VERSION,
        request_json=request,
        hash_version=2,
        request_hash=invoice_issuance_hash_v2(ledger, command, raw_body=request.encode()),
        idempotency_key=key,
        snapshot_json=frozen.content.decode(),
        snapshot_sha256=frozen.sha256,
    )
