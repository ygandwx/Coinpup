"""Preserve raw manual component spelling and source order in permanent v2 intents."""

from coinpup_api.ledger.idempotency import _v2_hash


def credit_hash_v2(ledger_id, payload, *, raw_body: bytes | None = None):
    return _v2_hash("business_credit", ledger_id, payload, raw_body)
