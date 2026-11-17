"""The business command shares the permanent ledger key namespace and v2 encoding."""

from coinpup_api.ledger.idempotency import _v2_hash


def confirmation_hash_v2(ledger_id, document_id, payload, *, raw_body: bytes | None = None):
    return _v2_hash("confirm_document", ledger_id, payload, raw_body, document_id=document_id)


def document_revision_hash_v2(
    action, ledger_id, document_id, payload, *, raw_body: bytes | None = None
):
    """Keep the reviewed source strings, explicit nulls, order and route in the permanent intent."""
    if action not in {"correct", "cancel"}:
        raise ValueError("Unknown confirmed document revision action")
    return _v2_hash(
        f"{action}_confirmed_document", ledger_id, payload, raw_body, document_id=document_id
    )
