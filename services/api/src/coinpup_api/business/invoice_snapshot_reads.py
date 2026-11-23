"""Verify saved render bytes without consulting mutable current company or document records."""

import hashlib
import hmac
import json
import re
from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field, StrictInt

from coinpup_api.business.document_current import DocumentCurrent
from coinpup_api.business.invoice_issuance_results import (
    FrozenIssuanceEvidence,
    Sha256,
    StoredVersion,
)
from coinpup_api.business.invoice_issuance_schemas import InvoiceIssuanceCreate
from coinpup_api.business.invoice_profiles import InvoiceProfileResponse
from coinpup_api.business.invoice_render_snapshot import (
    MAX_RENDER_SNAPSHOT_BYTES,
    _encode,
    _invalid,
    _prepare,
)
from coinpup_api.ledger.assets import AssetDefinition


class InvoiceLogoIdentity(FrozenIssuanceEvidence):
    file_id: UUID
    ledger_id: UUID
    reviewed_version: StoredVersion
    sha256: Sha256
    byte_size: Annotated[StrictInt, Field(ge=1, le=2**63 - 1)]
    media_type: Literal["image/png", "image/jpeg", "image/webp"]


def _saved_logo(raw, profile):
    if profile.logo_file_id is None:
        if raw is not None:
            raise _invalid()
        return None
    logo = InvoiceLogoIdentity.model_validate(raw)
    if (logo.file_id, logo.ledger_id, logo.reviewed_version) != (
        profile.logo_file_id,
        profile.ledger_id,
        profile.logo_file_version,
    ):
        raise _invalid()
    return logo.model_dump(mode="json")


def _not_exact(value):
    raise _invalid()


def read_invoice_render_input(content, *, expected_sha256):
    """Load canonical v1 input using the digest from an already authorized reservation.

    Matching a caller-supplied digest is not an authorization check. The caller must
    obtain both bytes and digest from the immutable owned record. This function
    checks byte identity, bounded format and internal reviewed facts only; it does
    not imply that a number has been committed or either PDF has been persisted.
    No current configuration or file metadata is fetched, so later archival or
    correction cannot silently rebind a pending reservation to new content.
    """
    if (
        type(content) is not bytes
        or not 1 <= len(content) <= MAX_RENDER_SNAPSHOT_BYTES
        or type(expected_sha256) is not str
        or re.fullmatch(r"[0-9a-f]{64}", expected_sha256) is None
        or not hmac.compare_digest(hashlib.sha256(content).hexdigest(), expected_sha256)
    ):
        raise _invalid()
    try:
        raw = json.loads(content, parse_float=_not_exact, parse_constant=_not_exact)
        current = DocumentCurrent.model_validate(raw["document"]["current"])
        asset = AssetDefinition(**raw["document"]["asset"])
        profile = InvoiceProfileResponse.model_validate(raw["profile"])
        command = InvoiceIssuanceCreate.model_validate(raw["intent"])
        prepared = _prepare(
            current,
            asset,
            profile,
            ledger_id=current.ledger_id,
            command=command,
            sequence=raw["number"]["sequence"],
        )
        rebuilt = _encode(*prepared, _saved_logo(raw["logo"], profile))
        # Reject unknown schema/template versions, duplicate keys, normalization,
        # extra fields and noncanonical encodings as well as mismatched facts.
        if rebuilt.content != content:
            raise _invalid()
        return rebuilt
    except (ValueError, TypeError, KeyError, UnicodeError, RecursionError):
        raise _invalid() from None
