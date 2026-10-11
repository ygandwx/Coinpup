"""Fictional confirmation requests preserve raw intent without changing old hashes."""

import hashlib
import json
from uuid import UUID

import pytest
from coinpup_api.business.confirmation_hash import confirmation_hash_v2
from coinpup_api.business.confirmation_schemas import BusinessConfirmationCreate
from coinpup_api.ledger.service import LedgerError
from pydantic import ValidationError

LEDGER, DOCUMENT, CONFIRMATION, OTHER = (
    UUID(f"00000000-0000-4000-8000-{number:012d}") for number in range(1, 5)
)
PAYLOAD = {"id": str(CONFIRMATION), "expected_version": 2}


def request():
    return BusinessConfirmationCreate.model_validate(PAYLOAD)


def digest(payload=None, *, ledger=LEDGER, document=DOCUMENT, raw=None):
    return confirmation_hash_v2(ledger, document, payload or request(), raw_body=raw)


def test_confirmation_envelope_is_explicit_and_matches_canonical_v2():
    expected = {
        "action": "confirm_document",
        "route": "/api/v1/ledgers/{ledger_id}/business-documents/{document_id}/confirm",
        "ledger_id": str(LEDGER),
        "document_id": str(DOCUMENT),
        "payload": PAYLOAD,
    }
    encoded = json.dumps(expected, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    assert digest() == hashlib.sha256(encoded.encode()).hexdigest()
    assert digest() == digest(raw=json.dumps(PAYLOAD, indent=4).encode())
    assert digest() == digest(raw=json.dumps(dict(reversed(list(PAYLOAD.items())))).encode())


def test_confirmation_scope_and_intent_cannot_be_reused():
    original = digest()
    assert original != digest(ledger=OTHER)
    assert original != digest(document=OTHER)
    assert original != digest(request().model_copy(update={"id": OTHER}))
    assert original != digest(request().model_copy(update={"expected_version": 3}))


@pytest.mark.parametrize("version", [None, True, False, "2", 2.0, 0, -1, 2147483647])
def test_version_is_a_required_positive_strict_integer(version):
    with pytest.raises(ValidationError):
        BusinessConfirmationCreate.model_validate({**PAYLOAD, "expected_version": version})


@pytest.mark.parametrize("field", ["id", "expected_version"])
def test_identity_and_reviewed_version_are_required(field):
    with pytest.raises(ValidationError):
        BusinessConfirmationCreate.model_validate({k: v for k, v in PAYLOAD.items() if k != field})


@pytest.mark.parametrize("field", ["amount", "tax_amount", "lines", "document_id", "kind"])
def test_client_cannot_inject_calculated_values_or_route_scope(field):
    with pytest.raises(ValidationError):
        BusinessConfirmationCreate.model_validate({**PAYLOAD, field: "fictional"})


@pytest.mark.parametrize("raw", [b"null", b"[]", b"1", b"true", b"{", b'{"x":NaN}'])
def test_invalid_raw_json_is_rejected_without_echoing_it(raw):
    with pytest.raises(LedgerError) as error:
        digest(raw=raw)
    assert error.value.code == "invalid_request"
    assert error.value.status == 422


def test_raw_json_is_not_rebuilt_from_a_changed_typed_request():
    raw = json.dumps(PAYLOAD).encode()
    changed = request().model_copy(update={"expected_version": 999})
    assert digest(changed, raw=raw) == digest()
    assert digest(changed) != digest()


def test_raw_uuid_spelling_is_part_of_the_original_request():
    # Pydantic accepts compact UUIDs, but v2 deliberately retains the submitted spelling.
    compact = {**PAYLOAD, "id": CONFIRMATION.hex}
    assert BusinessConfirmationCreate.model_validate(compact) == request()
    assert digest(raw=json.dumps(compact).encode()) != digest()


def response_payload():
    return {
        "kind": "business_confirmation",
        "id": str(CONFIRMATION),
        "ledger_id": str(LEDGER),
        "document_id": str(DOCUMENT),
        "source_version": 2,
        "document_version": 3,
        "postings": [
            {
                "line_id": str(OTHER),
                "line_no": 2,
                "operation_id": str(UUID(int=5)),
                "journal_id": str(UUID(int=6)),
            }
        ],
    }


def test_confirmation_receipt_json_round_trip_retains_the_complete_membership():
    from coinpup_api.business.confirmation_schemas import BusinessConfirmationResponse

    raw = response_payload()
    parsed = BusinessConfirmationResponse.model_validate(raw)
    assert parsed.model_dump(mode="json") == raw
    assert BusinessConfirmationResponse.model_validate_json(parsed.model_dump_json()) == parsed
    with pytest.raises(ValidationError):
        parsed.document_version = 4


@pytest.mark.parametrize("field", ["line_id", "line_no", "operation_id", "journal_id"])
def test_receipt_cannot_duplicate_any_member_identity(field):
    from coinpup_api.business.confirmation_schemas import BusinessConfirmationResponse

    raw = response_payload()
    second = {
        "line_id": str(UUID(int=7)),
        "line_no": 3,
        "operation_id": str(UUID(int=8)),
        "journal_id": str(UUID(int=9)),
    }
    second[field] = raw["postings"][0][field]
    raw["postings"].append(second)
    with pytest.raises(ValidationError):
        BusinessConfirmationResponse.model_validate(raw)


@pytest.mark.parametrize("case", ["empty", "reversed", "version", "extra", "bool_position"])
def test_invalid_receipt_shape_is_not_silently_accepted(case):
    from coinpup_api.business.confirmation_schemas import BusinessConfirmationResponse

    raw = response_payload()
    if case == "empty":
        raw["postings"] = []
    elif case == "version":
        raw["document_version"] = 4
    elif case == "extra":
        raw["amount"] = "110.00"
    elif case == "bool_position":
        raw["postings"][0]["line_no"] = True
    else:
        raw["postings"].append(
            {
                "line_id": str(UUID(int=7)),
                "line_no": 1,
                "operation_id": str(UUID(int=8)),
                "journal_id": str(UUID(int=9)),
            }
        )
    with pytest.raises(ValidationError):
        BusinessConfirmationResponse.model_validate(raw)
