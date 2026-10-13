"""New bank evidence entry keeps the fixed original-intent hash and strict receipt branch."""

import json
from uuid import uuid4

import pytest
from coinpup_api.ledger.service import LedgerError
from coinpup_api.ocr.confirmation_contracts import (
    ConfirmationCreate,
    ConfirmationResult,
    command_assets,
    confirmation_request,
)
from pydantic import TypeAdapter, ValidationError

from tests.unit.ocr.test_confirmation_contracts import raw
from tests.unit.ocr.test_payment_confirmation_contracts import envelope


def payment_body():
    value = raw()
    value["entry"] = dict(kind="link_payment", payment_id=str(uuid4()), expected_version=1)
    return value


def test_payment_link_locks_no_assets_and_preserves_raw_body_projection():
    value = payment_body()
    payload = ConfirmationCreate.model_validate(value)
    ledger, draft = uuid4(), uuid4()
    original, digest = confirmation_request(ledger, draft, payload, json.dumps(value).encode())
    assert original == value and command_assets(payload.entry) == []
    assert confirmation_request(ledger, draft, payload)[1] == digest
    for changed in (
        value | {"duplicate_ack": False},
        value | {"confirmed": value["confirmed"][::-1]},
    ):
        body = ConfirmationCreate.model_validate(changed)
        assert confirmation_request(ledger, draft, body, json.dumps(changed).encode())[1] != digest
    wrong = value | {"entry": value["entry"] | {"payment_id": str(uuid4())}}
    with pytest.raises(LedgerError) as error:
        confirmation_request(ledger, draft, payload, json.dumps(wrong).encode())
    assert error.value.code == "ocr_invalid_payload"


def test_discriminated_receipt_preserves_complete_200_original_payment():
    value = envelope(200)
    receipt = TypeAdapter(ConfirmationResult).validate_python(value)
    assert receipt.model_dump(mode="json") == value
    assert "operation" not in receipt.model_dump()
    with pytest.raises(ValidationError):
        TypeAdapter(ConfirmationResult).validate_python(value | {"action": "link"})
