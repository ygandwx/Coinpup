"""Read the fixed, owned bilingual completion from its stored evidence."""

from datetime import datetime
from uuid import UUID

from coinpup_api.business.invoice_completion_models import InvoiceCompletion
from coinpup_api.business.invoice_issuance_results import (
    InvoiceIssuanceComplete,
    InvoicePdfArtifact,
)
from coinpup_api.business.invoice_reservation_facts import verify_invoice_reservation
from coinpup_api.files.models import StoredFile
from coinpup_api.ledger.errors import MoneyError


def _invalid():
    return MoneyError(
        "invoice_completion_integrity", "The saved Invoice completion is inconsistent."
    )


def _same_json(expected, observed):
    # Bound traversal by the known receipt shape. Python equality alone equates
    # true, 1 and 1.0; JSONB key order does not carry any meaning.
    if type(observed) is not type(expected):
        return False
    if isinstance(expected, dict):
        return expected.keys() == observed.keys() and all(
            _same_json(value, observed[key]) for key, value in expected.items()
        )
    if isinstance(expected, list):
        return len(expected) == len(observed) and all(
            _same_json(left, right) for left, right in zip(expected, observed, strict=True)
        )
    return expected == observed


def verify_invoice_completion(reservation, completion, *, zh_file, en_file, owner_id, ledger_id):
    """Verify metadata from an authorized consistent read, without opening any private files.

    Later file archival, retitling or configuration changes do not change the
    original receipt. The caller verifies physical bytes separately when needed.
    This read neither publishes an artifact nor proves a transaction committed.
    """
    saved = verify_invoice_reservation(reservation, owner_id=owner_id, ledger_id=ledger_id)
    try:
        if (
            not isinstance(completion, InvoiceCompletion)
            or completion.id != saved.identity.id
            or completion.created_by != owner_id
            or completion.ledger_id != ledger_id
            or type(completion.version) is not int
            or completion.version != 1
            or type(completion.response_status) is not int
            or completion.response_status != 201
        ):
            raise _invalid()
        for timestamp in (reservation.created_at, completion.created_at):
            if not isinstance(timestamp, datetime) or timestamp.utcoffset() is None:
                raise _invalid()
        if completion.created_at < reservation.created_at:
            raise _invalid()
        artifacts = []
        for language, file_id, file in (
            ("zh", completion.zh_file_id, zh_file),
            ("en", completion.en_file_id, en_file),
        ):
            if (
                not isinstance(file_id, UUID)
                or not isinstance(file, StoredFile)
                or file.id != file_id
                or file.created_by != owner_id
                or file.ledger_id != ledger_id
            ):
                raise _invalid()
            artifacts.append(
                InvoicePdfArtifact(
                    language=language,
                    file_id=file_id,
                    sha256=file.sha256,
                    byte_size=file.byte_size,
                    media_type=file.detected_media_type,
                )
            )
        expected = InvoiceIssuanceComplete(
            **saved.identity.model_dump(), state="complete", artifacts=tuple(artifacts)
        )
        if not _same_json(expected.model_dump(mode="json"), completion.response):
            raise _invalid()
        return expected
    except (TypeError, ValueError, AttributeError, OverflowError):
        raise _invalid() from None
