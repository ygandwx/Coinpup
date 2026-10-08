"""Confirmation v2 preserves the exact provided JSON projection and typed financial rules."""

import json
from copy import deepcopy
from uuid import uuid4

import pytest
from coinpup_api.ledger.service import LedgerError
from coinpup_api.ocr.confirmation_contracts import (
    ConfirmationCreate,
    command_assets,
    confirmation_request,
)
from pydantic import ValidationError


def raw():
    return dict(
        intent_id=str(uuid4()),
        expected_version=1,
        target_ledger_id=str(uuid4()),
        target_file_id=str(uuid4()),
        confirmed=["header.total", "header.currency"],
        entry={
            "kind": "opening",
            "command": {
                "account_id": str(uuid4()),
                "asset_id": "USD",
                "amount": "1.00",
                "transaction_date": "2031-07-18",
            },
        },
    )


@pytest.mark.parametrize("change", ["default", "null", "empty", "amount", "order"])
def test_hash_distinguishes_original_intent_even_when_typed_values_match(change):
    first = raw()
    changed = deepcopy(first)
    if change == "default":
        changed["duplicate_ack"] = False
    elif change == "null":
        changed["entry"]["command"]["id"] = None
    elif change == "empty":
        changed["entry"]["command"]["description"] = ""
    elif change == "amount":
        changed["entry"]["command"]["amount"] = "1.0"
    else:
        changed["confirmed"].reverse()
    ledger, draft = uuid4(), uuid4()

    def digest(value):
        model = ConfirmationCreate.model_validate(value)
        provided, hashed = confirmation_request(ledger, draft, model, json.dumps(value).encode())
        assert provided == value
        assert confirmation_request(ledger, draft, model)[1] == hashed
        return hashed

    assert digest(first) != digest(changed)


@pytest.mark.parametrize("change", ["number", "bool_version", "bool_ack", "unknown", "duplicates"])
def test_confirmation_rejects_ambiguous_inputs(change):
    value = raw()
    if change == "number":
        value["entry"]["command"]["amount"] = 1.0
    elif change == "bool_version":
        value["expected_version"] = True
    elif change == "bool_ack":
        value["duplicate_ack"] = "true"
    elif change == "unknown":
        value["entry"]["kind"] = "invoice"
    else:
        value["confirmed"] *= 2
    with pytest.raises(ValidationError):
        ConfirmationCreate.model_validate(value)


def test_raw_bytes_must_match_the_typed_payload_and_assets_come_from_the_command():
    value = raw()
    model = ConfirmationCreate.model_validate(value)
    value["entry"]["command"]["amount"] = "2.00"
    with pytest.raises(LedgerError) as error:
        confirmation_request(uuid4(), uuid4(), model, json.dumps(value).encode())
    assert error.value.code == "ocr_invalid_payload"
    assert command_assets(model.entry) == ["USD"]


def test_exchange_locks_both_assets_and_all_fee_assets_once():
    value = raw()
    value["entry"] = {
        "kind": "exchange",
        "command": {
            "source_account_id": str(uuid4()),
            "source_asset_id": "USD",
            "source_amount": "10.00",
            "destination_account_id": str(uuid4()),
            "destination_asset_id": "EUR",
            "destination_amount": "9.00",
            "transaction_date": "2031-07-18",
            "fees": [
                {
                    "account_id": str(uuid4()),
                    "asset_id": "BTC",
                    "category_id": str(uuid4()),
                    "amount": "0.00000001",
                }
            ],
        },
    }
    assert command_assets(ConfirmationCreate.model_validate(value).entry) == ["BTC", "EUR", "USD"]
