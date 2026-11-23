"""Raw-value v2 issuance intent, independent from all existing financial envelopes."""

import json

from coinpup_api.business.invoice_issuance_schemas import InvoiceIssuanceCreate
from coinpup_api.ledger.idempotency import _canonical_hash, _invalid_constant
from coinpup_api.ledger.service import LedgerError

INVOICE_ISSUANCE_ROUTE = "/api/v1/ledgers/{ledger_id}/invoice-issuances"


def invoice_issuance_payload(payload, *, raw_body=None):
    """Retain original JSON values while proving they describe the validated command."""
    try:
        if not isinstance(payload, InvoiceIssuanceCreate):
            raise ValueError("Invalid command")
        checked = InvoiceIssuanceCreate.model_validate(payload.model_dump())
        provided = (
            checked.model_dump(mode="json")
            if raw_body is None
            else json.loads(
                raw_body, parse_float=_invalid_constant, parse_constant=_invalid_constant
            )
        )
        if (
            not isinstance(provided, dict)
            or InvoiceIssuanceCreate.model_validate(provided) != checked
        ):
            raise ValueError("The raw body describes a different command")
        return provided
    except (ValueError, TypeError, RecursionError):
        raise LedgerError("invalid_request", 422, "The command body must be valid JSON.") from None


def invoice_issuance_hash_v2(ledger_id, payload, *, raw_body=None):
    return _canonical_hash(
        {
            "action": "invoice_issuance",
            "route": INVOICE_ISSUANCE_ROUTE,
            "ledger_id": str(ledger_id),
            "payload": invoice_issuance_payload(payload, raw_body=raw_body),
        }
    )
