"""Independent raw-JSON v2 envelopes; no changes to legacy routes or receipt hashes."""

import json

from coinpup_api.ledger.idempotency import _canonical_hash, _invalid_constant
from coinpup_api.ledger.service import LedgerError

_ROUTES = {
    "correct": "/api/v1/ledgers/{ledger_id}/business-credits/{credit_id}/corrections",
    "cancel": "/api/v1/ledgers/{ledger_id}/business-credits/{credit_id}/cancellations",
}


def credit_revision_hash_v2(
    action, ledger_id, credit_id, payload, *, raw_body: bytes | None = None
):
    """Bind the stable group, not its first member, to the untouched submitted JSON."""
    try:
        if action not in _ROUTES:
            raise ValueError("Unknown credit revision action")
        provided = (
            payload.model_dump(mode="json", exclude_unset=True)
            if raw_body is None
            else json.loads(raw_body, parse_constant=_invalid_constant)
        )
        if not isinstance(provided, dict):
            raise ValueError("A command is a JSON object")
        return _canonical_hash(
            {
                "action": f"business_credit_{action}",
                "route": _ROUTES[action],
                "ledger_id": str(ledger_id),
                "credit_id": str(credit_id),
                "payload": provided,
            }
        )
    except (ValueError, TypeError, RecursionError):
        raise LedgerError("invalid_request", 422, "The command body must be valid JSON.") from None
