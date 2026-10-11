"""Separate route-bound intents in the existing permanent ledger key namespace."""

from coinpup_api.ledger.idempotency import _v2_hash


def payment_hash_v2(ledger_id, payload, *, raw_body: bytes | None = None):
    return _v2_hash("business_payment", ledger_id, payload, raw_body)


def allocation_hash_v2(ledger_id, payload, *, raw_body: bytes | None = None):
    return _v2_hash("business_allocation", ledger_id, payload, raw_body)
