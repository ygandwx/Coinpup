"""Verify saved reservation facts without allocating or inferring completion state."""

import hmac
import json
import re
from dataclasses import dataclass, field
from uuid import UUID

from coinpup_api.business.invoice_issuance_hash import (
    invoice_issuance_hash_v2,
    invoice_issuance_payload,
)
from coinpup_api.business.invoice_issuance_results import InvoiceIssuanceIdentity
from coinpup_api.business.invoice_issuance_schemas import InvoiceIssuanceCreate
from coinpup_api.business.invoice_numbering import MAX_SEQUENCE
from coinpup_api.business.invoice_render_snapshot import (
    MAX_RENDER_SNAPSHOT_BYTES,
    FrozenInvoiceRenderInput,
)
from coinpup_api.business.invoice_reservation_models import InvoiceReservation
from coinpup_api.business.invoice_snapshot_reads import read_invoice_render_input
from coinpup_api.ledger.errors import MoneyError
from coinpup_api.ledger.service import LedgerError


@dataclass(frozen=True, slots=True)
class VerifiedInvoiceReservation:
    identity: InvoiceIssuanceIdentity
    snapshot: FrozenInvoiceRenderInput = field(repr=False)
    idempotency_key: str = field(repr=False)
    request_hash: str
    request_json: str = field(repr=False)


def _invalid():
    return MoneyError("invoice_reservation_integrity", "The saved Invoice intent is inconsistent.")


def _bounded_text(value, maximum):
    if type(value) is not str or not 2 <= len(value) <= maximum:
        raise _invalid()
    raw = value.encode("utf-8")
    if len(raw) > maximum:
        raise _invalid()
    return raw


def verify_invoice_reservation(row, *, owner_id, ledger_id):
    """Check an already authorized row and return detached immutable evidence.

    Supplied owner/ledger values are not authentication. The caller first authorizes
    its query and must separately read completion/receipt state. This verifier does
    not consult mutable current document/configuration/File records or prove commit.
    """
    try:
        if (
            not isinstance(row, InvoiceReservation)
            or not isinstance(owner_id, UUID)
            or not isinstance(ledger_id, UUID)
            or row.created_by != owner_id
            or row.ledger_id != ledger_id
            or type(row.version) is not int
            or row.version != 1
            or type(row.hash_version) is not int
            or row.hash_version != 2
            or type(row.sequence) is not int
            or not 1 <= row.sequence <= MAX_SEQUENCE
            or type(row.idempotency_key) is not str
            or re.fullmatch(r"[!-~]{1,128}", row.idempotency_key) is None
            or type(row.request_hash) is not str
            or re.fullmatch(r"[0-9a-f]{64}", row.request_hash) is None
        ):
            raise _invalid()
        for name in ("id", "ledger_id", "created_by", "document_id", "configuration_id"):
            if not isinstance(getattr(row, name), UUID):
                raise _invalid()
        for name in ("previous_issuance_id", "logo_file_id"):
            if getattr(row, name) is not None and not isinstance(getattr(row, name), UUID):
                raise _invalid()
        frozen = read_invoice_render_input(
            _bounded_text(row.snapshot_json, MAX_RENDER_SNAPSHOT_BYTES),
            expected_sha256=row.snapshot_sha256,
        )
        data = frozen.decode()
        command = InvoiceIssuanceCreate.model_validate(data["intent"])
        if (
            row.id != command.id
            or row.document_id != command.document_id
            or type(row.document_version) is not int
            or row.document_version != command.expected_document_version
            or row.configuration_id != command.configuration_id
            or type(row.configuration_version) is not int
            or row.configuration_version != command.expected_configuration_version
            or row.previous_issuance_id != command.previous_issuance_id
            or str(ledger_id) != data["document"]["current"]["ledger_id"]
            or row.logo_file_id != (UUID(data["logo"]["file_id"]) if data["logo"] else None)
            or data["number"] != {"sequence": str(row.sequence), "number": row.number}
            or row.template_version != data["template_version"]
        ):
            raise _invalid()
        request = _bounded_text(row.request_json, 65536)
        original = invoice_issuance_payload(command, raw_body=request)
        canonical = json.dumps(original, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        if canonical != row.request_json:
            raise _invalid()
        digest = invoice_issuance_hash_v2(ledger_id, command, raw_body=request)
        if not hmac.compare_digest(row.request_hash, digest):
            raise _invalid()
        identity = InvoiceIssuanceIdentity(
            kind="invoice_issuance",
            id=row.id,
            ledger_id=ledger_id,
            document_id=row.document_id,
            document_version=row.document_version,
            configuration_id=row.configuration_id,
            configuration_version=row.configuration_version,
            previous_issuance_id=row.previous_issuance_id,
            numbering=data["profile"]["numbering"],
            sequence=str(row.sequence),
            number=row.number,
            snapshot_sha256=frozen.sha256,
            template_version=row.template_version,
        )
        return VerifiedInvoiceReservation(
            identity, frozen, row.idempotency_key, digest, row.request_json
        )
    except (
        ValueError,
        TypeError,
        KeyError,
        AttributeError,
        UnicodeError,
        RecursionError,
        LedgerError,
    ):
        raise _invalid() from None
