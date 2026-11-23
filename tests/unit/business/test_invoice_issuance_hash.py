"""Exact original request values bind one reviewed issuance and owned ledger."""

import json
from uuid import uuid4

import pytest
from coinpup_api.business.invoice_issuance_hash import (
    invoice_issuance_hash_v2,
    invoice_issuance_payload,
)
from coinpup_api.ledger.service import LedgerError

from tests.unit.business.test_invoice_document_snapshot import source


def test_key_order_is_irrelevant_but_original_uuid_spelling_and_owned_scope_are_bound():
    facts, command = source()
    raw = command.model_dump(mode="json")
    digest = invoice_issuance_hash_v2(facts.ledger_id, command)
    reversed_body = json.dumps(dict(reversed(list(raw.items())))).encode()
    assert invoice_issuance_hash_v2(facts.ledger_id, command, raw_body=reversed_body) == digest
    raw["id"] = raw["id"].upper()
    original = json.dumps(raw).encode()
    assert invoice_issuance_payload(command, raw_body=original) == raw
    assert invoice_issuance_hash_v2(facts.ledger_id, command, raw_body=original) != digest
    assert invoice_issuance_hash_v2(uuid4(), command) != digest
    other = command.model_copy(update={"previous_issuance_id": uuid4()})
    assert invoice_issuance_hash_v2(facts.ledger_id, other) != digest


@pytest.mark.parametrize("body", [b"{}", b"[]", b"null", b"{", b"NaN", b"1.0", b"\xff"])
def test_invalid_raw_body_is_rejected_with_generic_error(body):
    facts, command = source()
    with pytest.raises(LedgerError) as caught:
        invoice_issuance_hash_v2(facts.ledger_id, command, raw_body=body)
    assert (caught.value.code, caught.value.status) == ("invalid_request", 422)


@pytest.mark.parametrize(
    "field,value",
    [
        ("document_id", str(uuid4())),
        ("configuration_id", str(uuid4())),
        ("expected_document_version", 1.0),
        ("expected_configuration_version", True),
        ("unknown", "fictional"),
    ],
)
def test_raw_body_cannot_be_bound_to_a_different_or_unvalidated_command(field, value):
    facts, command = source()
    raw = command.model_dump(mode="json") | {field: value}
    with pytest.raises(LedgerError):
        invoice_issuance_hash_v2(facts.ledger_id, command, raw_body=json.dumps(raw).encode())
