"""Stable original command hashes and permanent receipt replay."""

import hashlib
import json
import re
from contextlib import contextmanager

from pydantic import TypeAdapter, ValidationError

from coinpup_api.ledger.models import CommandReceipt
from coinpup_api.ledger.posting_schemas import FinancialResponse
from coinpup_api.ledger.service import LedgerError

_IDEMPOTENCY_KEY = re.compile(r"[!-~]{1,128}")
_RECEIPT = TypeAdapter(FinancialResponse)
_COMMON_LEGACY_FIELDS = {"id", "asset_id", "amount", "transaction_date", "description"}
_LEGACY_HASH_FIELDS = {
    "opening": _COMMON_LEGACY_FIELDS | {"account_id"},
    "income": _COMMON_LEGACY_FIELDS | {"account_id", "recognition_date", "splits"},
    "expense": _COMMON_LEGACY_FIELDS | {"account_id", "recognition_date", "splits"},
    "transfer": _COMMON_LEGACY_FIELDS | {"source_account_id", "destination_account_id"},
}


def command_hash(kind, ledger_id, payload) -> str:
    """Hash normalized JSON structure, retaining the exact original amount strings.

    Server-generated IDs are absent from the hash. Omitted optional IDs and explicit
    null both serialize to null; retrying with the returned ID is a different command.
    """
    serialized = payload.model_dump(mode="json")
    if kind in _LEGACY_HASH_FIELDS:
        # Freeze the published command projection: new optional defaults must not invalidate
        # receipts issued before fees existed. Only a non-empty extension changes the hash.
        serialized = {
            field: serialized[field] for field in _LEGACY_HASH_FIELDS[kind] if field in serialized
        }
        if getattr(payload, "fees", None):
            serialized["fees"] = [item.model_dump(mode="json") for item in payload.fees]
    body = {"kind": kind, "ledger_id": str(ledger_id), "payload": serialized}
    encoded = json.dumps(
        body, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def revision_hash(action, ledger_id, operation_id, payload):
    value = {
        "action": action,
        "ledger_id": str(ledger_id),
        "operation_id": str(operation_id),
        "payload": payload.model_dump(mode="json"),
    }
    canonical = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class CommandIdempotency:
    @contextmanager
    def _command(self, owner_id, ledger_id, kind, payload, key):
        if not isinstance(key, str) or _IDEMPOTENCY_KEY.fullmatch(key) is None:
            raise LedgerError(
                "invalid_idempotency_key",
                422,
                "Use 1–128 visible ASCII characters for the command key.",
            )
        digest = command_hash(kind, ledger_id, payload)
        with self._transaction(owner_id) as session:
            _, entity = self._locked_ledger(session, owner_id, ledger_id)
            receipt = session.get(CommandReceipt, (ledger_id, key))
            if receipt is not None:
                if receipt.request_hash != digest:
                    raise LedgerError(
                        "idempotency_conflict",
                        409,
                        "This command key was used for a different request.",
                    )
                try:
                    response = _RECEIPT.validate_python(receipt.response)
                except ValidationError:
                    raise LedgerError(
                        "ledger_integrity", 503, "The stored command receipt is inconsistent."
                    ) from None
                yield session, digest, response
                return
            if entity.archived:
                raise LedgerError("entity_archived", 409, "Restore the entity before posting.")
            yield session, digest, None
