"""The business command shares the permanent ledger key namespace and v2 encoding."""

from coinpup_api.ledger.idempotency import _v2_hash


def confirmation_hash_v2(ledger_id, document_id, payload, *, raw_body: bytes | None = None):
    return _v2_hash("confirm_document", ledger_id, payload, raw_body, document_id=document_id)
