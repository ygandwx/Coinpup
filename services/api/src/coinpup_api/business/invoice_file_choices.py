"""Select canonical PDF identities and verify the selected private bytes before sealing."""

import re
from dataclasses import dataclass, field
from uuid import UUID

from sqlalchemy import and_, or_, select

from coinpup_api.business.invoice_artifacts import PreparedInvoiceBlob, PreparedInvoiceBlobs
from coinpup_api.business.invoice_pdf import MAX_INVOICE_PDF_BYTES
from coinpup_api.business.invoice_reservation_facts import verify_invoice_reservation
from coinpup_api.business.invoice_reservation_models import InvoiceReservation
from coinpup_api.files.models import StoredFile
from coinpup_api.files.storage import FileStoreError
from coinpup_api.ledger.errors import MoneyError
from coinpup_api.ledger.service import LedgerError, LedgerService, _not_found


@dataclass(frozen=True, slots=True)
class InvoiceFileChoice:
    language: str
    sha256: str
    byte_size: int
    blob_key: str = field(repr=False)
    existing_file_id: UUID | None


@dataclass(frozen=True, slots=True)
class InvoiceFileChoices:
    owner_id: UUID
    ledger_id: UUID
    issuance_id: UUID
    snapshot_sha256: str
    artifacts: tuple[InvoiceFileChoice, InvoiceFileChoice]


def _integrity():
    return LedgerError(
        "invoice_artifact_integrity", 503, "The private Invoice originals are inconsistent."
    )


def _select_choices(session, owner, ledger, identifier, prepared):
    """Internal caller has authorized the company; prepared comes only from the renderer."""
    row = session.scalar(
        select(InvoiceReservation).where(
            InvoiceReservation.id == identifier,
            InvoiceReservation.ledger_id == ledger,
            InvoiceReservation.created_by == owner,
        )
    )
    if row is None:
        raise _not_found()
    try:
        saved = verify_invoice_reservation(row, owner_id=owner, ledger_id=ledger)
    except MoneyError as error:
        raise LedgerError(error.code, 503, "The stored Invoice intent is inconsistent.") from None
    if (
        type(prepared) is not PreparedInvoiceBlobs
        or prepared.snapshot_sha256 != saved.snapshot.sha256
        or type(prepared.artifacts) is not tuple
        or len(prepared.artifacts) != 2
    ):
        raise _integrity()
    for language, blob in zip(("zh", "en"), prepared.artifacts, strict=True):
        if (
            type(blob) is not PreparedInvoiceBlob
            or blob.language != language
            or blob.media_type != "application/pdf"
            or type(blob.byte_size) is not int
            or not 1 <= blob.byte_size <= MAX_INVOICE_PDF_BYTES
            or type(blob.sha256) is not str
            or not re.fullmatch(r"[0-9a-f]{64}", blob.sha256)
            or type(blob.blob_key) is not str
            or not re.fullmatch(r"[0-9a-f]{32}", blob.blob_key)
        ):
            raise _integrity()
    zh, en = prepared.artifacts
    if zh.blob_key == en.blob_key or (zh.sha256, zh.byte_size) == (en.sha256, en.byte_size):
        raise _integrity()
    candidates = session.scalars(
        select(StoredFile).where(
            StoredFile.ledger_id == ledger,
            or_(
                *(
                    and_(StoredFile.sha256 == blob.sha256, StoredFile.byte_size == blob.byte_size)
                    for blob in prepared.artifacts
                )
            ),
        )
    )
    files = {(item.sha256, item.byte_size): item for item in candidates}
    result = []
    for blob in prepared.artifacts:
        existing = files.get((blob.sha256, blob.byte_size))
        if existing is not None:
            if existing.created_by != owner or existing.detected_media_type != "application/pdf":
                raise _integrity()
            if existing.archived:
                raise LedgerError(
                    "file_archived", 409, "Restore the canonical private file before sealing."
                )
        result.append(
            InvoiceFileChoice(
                blob.language,
                blob.sha256,
                blob.byte_size,
                existing.blob_key if existing else blob.blob_key,
                existing.id if existing else None,
            )
        )
    if result[0].blob_key == result[1].blob_key:
        raise _integrity()
    return InvoiceFileChoices(owner, ledger, identifier, saved.snapshot.sha256, tuple(result))


def _verify_selected_bytes(store, choices):
    # Use FileStore's whole-file size/SHA verification and read the same pinned handles.
    try:
        for file in choices.artifacts:
            with store.open_blob(file.blob_key, file.sha256, file.byte_size) as stream:
                if file.byte_size < 11 or stream.read(5) != b"%PDF-":
                    raise _integrity()
                stream.seek(-6, 2)
                if stream.read() != b"%%EOF\n":
                    raise _integrity()
    except (FileStoreError, OSError):
        raise _integrity() from None


class InvoiceOriginalFiles(LedgerService):
    def select_verified(self, owner, ledger, identifier, prepared, *, store):
        with self._transaction(owner, read_only=True) as session:
            _, entity = self._ledger(session, owner, ledger)
            if entity.kind != "company":
                raise LedgerError(
                    "invoice_requires_company", 422, "Invoice issuance requires a company."
                )
            choices = _select_choices(session, owner, ledger, identifier, prepared)
        _verify_selected_bytes(store, choices)
        # Selection and byte verification do not write a file row or complete the issuance.
        # The sealing transaction must compare these exact identities again under its locks.
        return choices
