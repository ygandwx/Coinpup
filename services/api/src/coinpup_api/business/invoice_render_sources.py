"""Load original immutable render input; no rebuilding from current configuration."""

import hashlib
from dataclasses import dataclass, field
from uuid import UUID

from sqlalchemy import select

from coinpup_api.business.invoice_logo import MAX_LOGO_BYTES
from coinpup_api.business.invoice_render_snapshot import FrozenInvoiceRenderInput
from coinpup_api.business.invoice_reservation_facts import verify_invoice_reservation
from coinpup_api.business.invoice_reservation_models import InvoiceReservation
from coinpup_api.business.invoice_snapshot_reads import InvoiceLogoIdentity
from coinpup_api.files.models import StoredFile
from coinpup_api.files.storage import FileStoreError
from coinpup_api.ledger.errors import MoneyError
from coinpup_api.ledger.service import LedgerError, LedgerService, _not_found


@dataclass(frozen=True, slots=True)
class _StoredLogo:
    identity: InvoiceLogoIdentity
    blob_key: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class InvoiceRenderSource:
    ledger_id: UUID
    issuance_id: UUID
    snapshot: FrozenInvoiceRenderInput = field(repr=False)
    logo_content: bytes | None = field(repr=False)


def _invalid():
    return LedgerError(
        "invoice_logo_integrity", 503, "The original private Invoice Logo is unavailable."
    )


def _source_metadata(session, owner, ledger, identifier):
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
    raw = saved.snapshot.decode()["logo"]
    if raw is None:
        return saved.snapshot, None
    logo = InvoiceLogoIdentity.model_validate(raw)
    file = session.scalar(
        select(StoredFile).where(
            StoredFile.id == logo.file_id,
            StoredFile.ledger_id == ledger,
            StoredFile.created_by == owner,
        )
    )
    if (
        file is None
        or file.version < logo.reviewed_version
        or (file.sha256, file.byte_size, file.detected_media_type)
        != (logo.sha256, logo.byte_size, logo.media_type)
    ):
        raise _invalid()
    if logo.byte_size > MAX_LOGO_BYTES:
        raise LedgerError(
            "invoice_render_logo_limit",
            422,
            "The original Invoice Logo exceeds the rendering limit.",
        )
    return saved.snapshot, _StoredLogo(logo, file.blob_key)


def _read_original(store, original):
    if original is None:
        return None
    identity = original.identity
    try:
        with store.open_blob(original.blob_key, identity.sha256, identity.byte_size) as stream:
            content = stream.read(identity.byte_size + 1)
    except (FileStoreError, OSError):
        raise _invalid() from None
    if len(content) != identity.byte_size or hashlib.sha256(content).hexdigest() != identity.sha256:
        raise _invalid()
    return content


class InvoiceRenderSources(LedgerService):
    def load(self, owner, ledger, identifier, *, store):
        with self._transaction(owner, read_only=True) as session:
            _, entity = self._ledger(session, owner, ledger)
            if entity.kind != "company":
                raise LedgerError(
                    "invoice_requires_company", 422, "Invoice issuance requires a company."
                )
            snapshot, original = _source_metadata(session, owner, ledger, identifier)
        return InvoiceRenderSource(ledger, identifier, snapshot, _read_original(store, original))
