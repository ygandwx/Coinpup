"""Bounded immutable input for future bilingual rendering; this does not reserve a number."""

import hashlib
import json
import re
from dataclasses import dataclass
from uuid import UUID

from pydantic import ValidationError

from coinpup_api.business.invoice_document_snapshot import (
    MAX_DOCUMENT_SNAPSHOT_BYTES,
    freeze_invoice_document,
)
from coinpup_api.business.invoice_issuance_schemas import InvoiceIssuanceCreate
from coinpup_api.business.invoice_numbering import candidate_number
from coinpup_api.business.invoice_profiles import InvoiceProfileResponse
from coinpup_api.files.schemas import FileResponse
from coinpup_api.ledger.errors import MoneyError

INVOICE_TEMPLATE_VERSION = "commercial-invoice-v1"
MAX_RENDER_SNAPSHOT_BYTES = MAX_DOCUMENT_SNAPSHOT_BYTES + 64 * 1024


@dataclass(frozen=True, slots=True)
class FrozenInvoiceRenderInput:
    content: bytes

    @property
    def sha256(self):
        return hashlib.sha256(self.content).hexdigest()

    def decode(self):
        return json.loads(self.content)


def _invalid():
    return MoneyError("invoice_render_integrity", "The reviewed Invoice inputs are inconsistent.")


def _logo_identity(logo, profile, ledger_id):
    if profile.logo_file_id is None:
        if logo is not None:
            raise _invalid()
        return None
    if not isinstance(logo, FileResponse):
        raise _invalid()
    # FileResponse is a read DTO. Validate immutable identity before any coercion;
    # editable title and filename are deliberately not part of the render input.
    if (
        logo.id != profile.logo_file_id
        or logo.ledger_id != ledger_id
        or type(logo.version) is not int
        or not 1 <= logo.version <= 2147483647
        or logo.version < profile.logo_file_version
        or logo.archived is not False
        or type(logo.byte_size) is not int
        or not 1 <= logo.byte_size <= 2**63 - 1
        or type(logo.sha256) is not str
        or re.fullmatch(r"[0-9a-f]{64}", logo.sha256) is None
        or logo.detected_media_type not in {"image/png", "image/jpeg", "image/webp"}
    ):
        raise _invalid()
    return {
        "file_id": str(logo.id),
        "ledger_id": str(ledger_id),
        "reviewed_version": profile.logo_file_version,
        "sha256": logo.sha256,
        "byte_size": logo.byte_size,
        "media_type": logo.detected_media_type,
    }


def freeze_invoice_render_input(
    current, asset, profile, *, ledger_id, command, sequence, logo=None
):
    """Freeze server-read facts together, without performing writes or opening any file.

    The reservation service must first authorize the company and lock its ledger,
    entity and sequence. It must read the current document, configuration and Logo
    within that transaction, verify historical number uniqueness and persist this
    input atomically with the reservation. This helper only checks consistency.
    Subsequent rendering must verify actual Logo bytes against this saved identity;
    it must not rebuild a pending input from newer profile or document versions.
    """
    try:
        document, checked, reviewed, number = _prepare(
            current, asset, profile, ledger_id=ledger_id, command=command, sequence=sequence
        )
        return _encode(
            document, checked, reviewed, number, _logo_identity(logo, checked, ledger_id)
        )
    except (ValidationError, TypeError, ValueError, UnicodeError):
        raise _invalid() from None


def _prepare(current, asset, profile, *, ledger_id, command, sequence):
    if (
        not isinstance(profile, InvoiceProfileResponse)
        or not isinstance(command, InvoiceIssuanceCreate)
        or not isinstance(ledger_id, UUID)
    ):
        raise _invalid()
    source = profile.model_dump()
    checked = InvoiceProfileResponse.model_validate(source)
    reviewed = InvoiceIssuanceCreate.model_validate(command.model_dump())
    if (
        checked.model_dump() != source
        or type(profile.version) is not int
        or not 1 <= profile.version <= 2147483647
        or profile.archived is not False
        or checked.ledger_id != ledger_id
        or checked.id != reviewed.configuration_id
        or checked.version != reviewed.expected_configuration_version
    ):
        raise _invalid()
    document = freeze_invoice_document(current, asset, ledger_id=ledger_id, command=reviewed)
    number = candidate_number(checked.numbering, sequence)
    return document, checked, reviewed, number


def _encode(document, profile, intent, number, logo):
    payload = {
        "schema_version": 1,
        "template_version": INVOICE_TEMPLATE_VERSION,
        "intent": intent.model_dump(mode="json"),
        "number": {"sequence": number.sequence, "number": number.number},
        "document": document.decode(),
        "profile": profile.model_dump(mode="json"),
        "logo": logo,
    }
    content = json.dumps(
        payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")
    if len(content) > MAX_RENDER_SNAPSHOT_BYTES:
        raise _invalid()
    return FrozenInvoiceRenderInput(content)
