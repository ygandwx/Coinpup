"""Frozen v1 projections and raw-JSON v2 hashes for permanent receipt replay."""

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
_EXCHANGE_V1_FIELDS = {
    "id",
    "transaction_date",
    "description",
    "source_account_id",
    "source_asset_id",
    "source_amount",
    "destination_account_id",
    "destination_asset_id",
    "destination_amount",
}
_FEE_V1_FIELDS = {"account_id", "asset_id", "amount", "category_id"}
_SPLIT_V1_FIELDS = {"category_id", "amount"}
_ROUTES = {
    "opening": "/api/v1/ledgers/{ledger_id}/opening-balances",
    "income": "/api/v1/ledgers/{ledger_id}/income",
    "expense": "/api/v1/ledgers/{ledger_id}/expenses",
    "transfer": "/api/v1/ledgers/{ledger_id}/transfers",
    "exchange": "/api/v1/ledgers/{ledger_id}/exchanges",
    "correct": "/api/v1/ledgers/{ledger_id}/operations/{operation_id}/corrections",
    "cancel": "/api/v1/ledgers/{ledger_id}/operations/{operation_id}/cancellations",
}


def _canonical_hash(value):
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _v1_posting(kind, provided, *, replacement=False):
    # These field sets and defaults describe already-issued v1 receipts, not current schemas.
    fields = _EXCHANGE_V1_FIELDS if kind == "exchange" else _LEGACY_HASH_FIELDS[kind]
    result = {field: provided[field] for field in fields if field in provided}
    result.setdefault("description", "")
    if replacement:
        result.pop("id", None)
        result["kind"] = kind
    else:
        result.setdefault("id", None)
    if "splits" in result:
        result["splits"] = [
            {field: item[field] for field in _SPLIT_V1_FIELDS} for item in result["splits"]
        ]
    if kind != "opening":
        fees = provided.get("fees", [])
        if fees or kind == "exchange" or replacement:
            result["fees"] = [{field: item[field] for field in _FEE_V1_FIELDS} for item in fees]
    return result


def command_hash(kind, ledger_id, payload) -> str:
    """Frozen v1 hash, including its original defaults and nested field projections."""
    return _canonical_hash(
        {
            "kind": kind,
            "ledger_id": str(ledger_id),
            "payload": _v1_posting(kind, payload.model_dump(mode="json", exclude_unset=True)),
        }
    )


def revision_hash(action, ledger_id, operation_id, payload):
    """Frozen v1 revision envelope; replacement defaults differ from create defaults."""
    provided = payload.model_dump(mode="json", exclude_unset=True)
    serialized = {
        field: provided[field] for field in ("expected_version", "reason") if field in provided
    }
    if "replacement" in provided:
        replacement = provided["replacement"]
        serialized["replacement"] = _v1_posting(
            payload.replacement.kind, replacement, replacement=True
        )
    return _canonical_hash(
        {
            "action": action,
            "ledger_id": str(ledger_id),
            "operation_id": str(operation_id),
            "payload": serialized,
        }
    )


def _invalid_constant(value):
    raise ValueError("Non-finite JSON number")


def _v2_hash(action, ledger_id, payload, raw_body, *, operation_id=None):
    try:
        # HTTP always supplies actual cached bytes; typed internal callers retain fields_set.
        provided = (
            payload.model_dump(mode="json", exclude_unset=True)
            if raw_body is None
            else json.loads(raw_body, parse_constant=_invalid_constant)
        )
        if not isinstance(provided, dict):
            raise ValueError("A command is a JSON object")
        value = {
            "action": action,
            "route": _ROUTES[action],
            "ledger_id": str(ledger_id),
            "payload": provided,
        }
        if operation_id is not None:
            value["operation_id"] = str(operation_id)
        return _canonical_hash(value)
    except (ValueError, TypeError, RecursionError):
        raise LedgerError("invalid_request", 422, "The command body must be valid JSON.") from None


def command_hash_v2(kind, ledger_id, payload, *, raw_body: bytes | None = None):
    return _v2_hash(kind, ledger_id, payload, raw_body)


def revision_hash_v2(action, ledger_id, operation_id, payload, *, raw_body: bytes | None = None):
    return _v2_hash(action, ledger_id, payload, raw_body, operation_id=operation_id)


def receipt_hash_version(receipt):
    if receipt is None:
        return 2
    if receipt.hash_version not in (1, 2):
        raise LedgerError("ledger_integrity", 503, "The stored command receipt is inconsistent.")
    return receipt.hash_version


class CommandIdempotency:
    @contextmanager
    def _command(self, owner_id, ledger_id, kind, payload, key, *, raw_body=None):
        if not isinstance(key, str) or _IDEMPOTENCY_KEY.fullmatch(key) is None:
            raise LedgerError(
                "invalid_idempotency_key",
                422,
                "Use 1–128 visible ASCII characters for the command key.",
            )
        with self._transaction(owner_id, write=True) as session:
            _, entity = self._locked_ledger(session, owner_id, ledger_id)
            receipt = session.get(CommandReceipt, (ledger_id, key))
            version = receipt_hash_version(receipt)
            digest = (
                command_hash(kind, ledger_id, payload)
                if version == 1
                else command_hash_v2(kind, ledger_id, payload, raw_body=raw_body)
            )
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
