"""Device sync retains the exact nested JSON identity of established financial commands."""

import json
from uuid import uuid4

import pytest
from coinpup_api.ledger.idempotency import command_hash_v2, revision_hash_v2
from coinpup_api.ledger.posting_schemas import CorrectionCreate, ExpenseCreate
from coinpup_api.ledger.service import LedgerError
from coinpup_api.sync.financial_push import (
    FinancialPushCommand,
    decode_push_json,
    freeze_financial_push,
)
from pydantic import TypeAdapter


def _expense():
    return {
        "id": str(uuid4()),
        "account_id": str(uuid4()),
        "asset_id": "USD",
        "amount": "10.0",
        "splits": [{"category_id": str(uuid4()), "amount": "10.00"}],
        "transaction_date": "2026-11-01",
        "recognition_date": "2026-10-31",
    }


def test_stable_ids_and_the_whitelist_are_part_of_the_schema():
    schema = TypeAdapter(FinancialPushCommand).json_schema()
    assert set(schema["discriminator"]["mapping"]) == {
        "opening",
        "income",
        "expense",
        "transfer",
        "exchange",
        "correct",
        "cancel",
    }
    for kind in ("Opening", "Income", "Expense", "Transfer", "Exchange"):
        assert "id" in schema["$defs"][f"Push{kind}Payload"]["required"]
    for kind in ("Correction", "Cancellation"):
        assert "operation_id" in schema["$defs"][f"Push{kind}"]["required"]


def test_create_preserves_the_same_ledger_hash_and_raw_amount_strings():
    ledger = uuid4()
    payload = _expense()
    raw = json.dumps({"command": {"kind": "expense", "payload": payload}}).encode()
    original = decode_push_json(raw)["command"]
    frozen = freeze_financial_push(ledger, original)
    assert frozen.operation_id == frozen.command.payload.id
    assert frozen.expected_version is None
    assert json.loads(frozen.original_payload) == payload
    assert frozen.ledger_digest == command_hash_v2(
        "expense",
        ledger,
        ExpenseCreate.model_validate(payload),
        raw_body=json.dumps(payload).encode(),
    )
    rewrites = [
        original | {"payload": payload | {"amount": "10.00"}},
        original | {"payload": payload | {"fees": []}},
        original | {"payload": payload | {"description": ""}},
    ]
    assert all(
        freeze_financial_push(ledger, item).ledger_digest != frozen.ledger_digest
        for item in rewrites
    )


def test_revision_does_not_normalize_original_reason_or_version():
    ledger, operation = uuid4(), uuid4()
    replacement = _expense()
    del replacement["id"]
    payload = {
        "reason": " Fictional corrected amount ",
        "expected_version": 2,
        "replacement": replacement | {"kind": "expense"},
    }
    frozen = freeze_financial_push(
        ledger, {"kind": "correct", "operation_id": str(operation), "payload": payload}
    )
    assert frozen.expected_version == 2
    assert frozen.command.payload.reason == "Fictional corrected amount"
    assert json.loads(frozen.original_payload)["reason"] == payload["reason"]
    assert frozen.ledger_digest == revision_hash_v2(
        "correct",
        ledger,
        operation,
        CorrectionCreate.model_validate(payload),
        raw_body=json.dumps(payload).encode(),
    )


@pytest.mark.parametrize("data", [b"", b"[]", b'{"a":{"x":1,"x":2}}', b'{"x":NaN}', b" " * 131_073])
def test_envelope_rejects_ambiguous_or_unbounded_json(data):
    with pytest.raises(LedgerError) as caught:
        decode_push_json(data)
    assert (caught.value.code, caught.value.status) == ("invalid_financial_push", 422)


@pytest.mark.parametrize(
    "change",
    [
        {"kind": "business_document"},
        {"operation_id": str(uuid4())},
        {"payload": _expense() | {"amount": 10.0}},
        {"payload": _expense() | {"id": None}},
        {"payload": {k: v for k, v in _expense().items() if k != "id"}},
    ],
)
def test_rejects_another_command_or_unstable_creation_identity(change):
    with pytest.raises(LedgerError) as caught:
        freeze_financial_push(uuid4(), {"kind": "expense", "payload": _expense()} | change)
    assert caught.value.code == "invalid_financial_push"
