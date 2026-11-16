"""Hash the raw command for one allocation return revision."""

import json

from coinpup_api.ledger.idempotency import _canonical_hash, _invalid_constant
from coinpup_api.ledger.service import LedgerError

_ROUTES = {
    "correct": "/api/v1/ledgers/{ledger_id}/business-allocation-returns/{return_id}/corrections",
    "cancel": "/api/v1/ledgers/{ledger_id}/business-allocation-returns/{return_id}/cancellations",
}


def allocation_return_revision_hash_v2(
    action, ledger_id, return_id, payload, *, raw_body: bytes | None = None
):
    """Bind the original fields, ordered arrays and amount spellings to this return."""
    try:
        if action not in _ROUTES:
            raise ValueError("Unknown allocation return revision action")
        provided = (
            payload.model_dump(mode="json", exclude_unset=True)
            if raw_body is None
            else json.loads(raw_body, parse_constant=_invalid_constant)
        )
        if not isinstance(provided, dict):
            raise ValueError("A command is a JSON object")
        return _canonical_hash(
            {
                "action": f"business_allocation_return_{action}",
                "route": _ROUTES[action],
                "ledger_id": str(ledger_id),
                "return_id": str(return_id),
                "payload": provided,
            }
        )
    except (ValueError, TypeError, RecursionError):
        raise LedgerError("invalid_request", 422, "The command body must be valid JSON.") from None
