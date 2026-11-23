"""Stored-row consistency only; fictional ORM objects do not prove a committed reservation."""

import hashlib
import json
from dataclasses import FrozenInstanceError
from uuid import UUID

import pytest
from coinpup_api.business.invoice_issuance_hash import invoice_issuance_hash_v2
from coinpup_api.business.invoice_numbering import MAX_SEQUENCE
from coinpup_api.business.invoice_reservation_facts import verify_invoice_reservation
from coinpup_api.business.invoice_reservation_models import InvoiceReservation
from coinpup_api.ledger.errors import MoneyError
from pydantic import ValidationError

from tests.unit.business.test_invoice_render_snapshot import freeze, inputs

OWNER = UUID(int=700)
OTHER = UUID(int=999)


def saved(count=3, with_logo=False, sequence="1", uppercase_request=False, previous=None):
    args = inputs(count, with_logo=with_logo)
    facts, command, profile, logo = args
    command.id = UUID("aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee")
    command.previous_issuance_id = previous
    frozen = freeze(*args, sequence=sequence)
    raw = command.model_dump(mode="json")
    if uppercase_request:
        raw["id"] = raw["id"].upper()
    request = json.dumps(raw, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    data = frozen.decode()
    row = InvoiceReservation(
        id=command.id,
        ledger_id=facts.ledger_id,
        created_by=OWNER,
        version=1,
        document_id=command.document_id,
        document_version=command.expected_document_version,
        configuration_id=command.configuration_id,
        configuration_version=command.expected_configuration_version,
        previous_issuance_id=command.previous_issuance_id,
        logo_file_id=logo.id if logo else None,
        sequence=int(sequence),
        number=data["number"]["number"],
        template_version=data["template_version"],
        snapshot_json=frozen.content.decode(),
        snapshot_sha256=frozen.sha256,
        request_json=request,
        request_hash=invoice_issuance_hash_v2(facts.ledger_id, command, raw_body=request.encode()),
        hash_version=2,
        idempotency_key="fictional-invoice-original-key",
    )
    return row, args


def verify(row, **scope):
    return verify_invoice_reservation(
        row, owner_id=scope.get("owner", OWNER), ledger_id=scope.get("ledger", row.ledger_id)
    )


@pytest.mark.parametrize("count", [1, 200])
@pytest.mark.parametrize("with_logo", [False, True])
def test_detached_snapshot_and_identity_keep_all_original_facts(count, with_logo):
    row, args = saved(count, with_logo)
    result = verify(row)
    assert result.snapshot.content.decode() == row.snapshot_json
    assert result.identity.id == row.id and result.identity.ledger_id == row.ledger_id
    assert result.identity.document_version == row.document_version
    assert result.request_hash == row.request_hash and result.idempotency_key == row.idempotency_key
    assert result.request_json == row.request_json
    assert len(result.snapshot.decode()["document"]["current"]["document"]["lines"]) == count
    assert "Fictional" not in repr(result) and row.idempotency_key not in repr(result)
    before = result.identity.model_dump(mode="json"), result.snapshot.content
    row.number = "Later object mutation"
    row.snapshot_json = "{}"
    args[0].document.lines.clear()
    args[2].archived = True
    args[2].version += 1
    result.snapshot.decode()["profile"]["name"] = "Later decoded mutation"
    assert (result.identity.model_dump(mode="json"), result.snapshot.content) == before
    with pytest.raises(FrozenInstanceError):
        result.request_hash = "f" * 64
    with pytest.raises(ValidationError):
        result.identity.numbering.minimum_digits = 2
    # Reservation existence alone must not manufacture either state or a completion receipt.
    assert not hasattr(result, "state") and not hasattr(result, "artifacts")


@pytest.mark.parametrize("sequence", ["1", "100000", str(MAX_SEQUENCE)])
def test_exact_sequence_and_number_are_read_without_allocating_the_next(sequence):
    row, _ = saved(sequence=sequence)
    row.idempotency_key = "~" * 128
    result = verify(row)
    assert result.identity.sequence == sequence and result.identity.number == row.number


def test_reissue_keeps_the_original_predecessor_in_all_three_saved_facts():
    previous = UUID(int=111)
    row, _ = saved(previous=previous)
    result = verify(row)
    assert result.identity.previous_issuance_id == previous
    assert result.snapshot.decode()["intent"]["previous_issuance_id"] == str(previous)
    assert json.loads(result.request_json)["previous_issuance_id"] == str(previous)


def test_original_uuid_spelling_is_valid_but_must_keep_its_original_request_hash():
    row, _ = saved(uppercase_request=True)
    result = verify(row)
    assert "AAAAAAAA-BBBB-4CCC-8DDD-EEEEEEEEEEEE" in result.request_json
    row.request_json = row.request_json.lower()
    with pytest.raises(MoneyError) as caught:
        verify(row)
    assert caught.value.code == "invoice_reservation_integrity"


@pytest.mark.parametrize("scope", [{"owner": OTHER}, {"ledger": OTHER}, {"owner": str(OWNER)}])
def test_wrong_authorized_scope_is_not_read_as_this_reservation(scope):
    row, _ = saved()
    with pytest.raises(MoneyError) as caught:
        verify(row, **scope)
    assert caught.value.code == "invoice_reservation_integrity"
    assert str(row.id) not in str(caught.value)


def test_changing_row_and_requested_ledger_together_still_cannot_rebind_the_snapshot():
    row, _ = saved()
    row.ledger_id = OTHER
    with pytest.raises(MoneyError):
        verify(row, ledger=OTHER)


@pytest.mark.parametrize(
    "field,value",
    [
        ("id", OTHER),
        ("document_id", OTHER),
        ("configuration_id", OTHER),
        ("created_by", OTHER),
        ("document_version", 3),
        ("document_version", 2.0),
        ("configuration_version", 999),
        ("configuration_version", True),
        ("previous_issuance_id", OTHER),
        ("logo_file_id", OTHER),
        ("sequence", 2),
        ("sequence", True),
        ("sequence", 1.0),
        ("sequence", 0),
        ("sequence", MAX_SEQUENCE + 1),
        ("number", "Fictional changed"),
        ("template_version", "changed-v2"),
        ("version", True),
        ("version", 2),
        ("hash_version", 2.0),
        ("hash_version", 1),
        ("request_hash", "f" * 64),
        ("request_hash", "F" * 64),
        ("snapshot_sha256", "f" * 64),
        ("request_json", "{}"),
        ("snapshot_json", "{}"),
        ("request_json", b"{}"),
        ("idempotency_key", ""),
        ("idempotency_key", "contains space"),
        ("idempotency_key", "x" * 129),
        ("idempotency_key", "虚构"),
        ("id", str(OTHER)),
        ("logo_file_id", str(OTHER)),
    ],
)
def test_mismatched_stored_facts_fail_without_returning_any_identity(field, value):
    row, _ = saved()
    setattr(row, field, value)
    with pytest.raises(MoneyError) as caught:
        verify(row)
    assert caught.value.code == "invoice_reservation_integrity"


def test_bound_logo_is_required_even_if_current_file_was_later_archived():
    row, args = saved(with_logo=True)
    args[3].archived = True
    assert verify(row).snapshot.decode()["logo"]["file_id"] == str(row.logo_file_id)
    row.logo_file_id = None
    with pytest.raises(MoneyError):
        verify(row)


@pytest.mark.parametrize(
    "change", ["float-version", "wrong-intent", "unknown-field", "nan", "truncated"]
)
def test_original_request_must_still_describe_the_frozen_intent(change):
    row, _ = saved()
    raw = json.loads(row.request_json)
    if change == "float-version":
        raw["expected_document_version"] = float(raw["expected_document_version"])
    elif change == "wrong-intent":
        raw["id"] = str(OTHER)
    elif change == "unknown-field":
        raw["client_number"] = "Fictional overwrite"
    elif change == "nan":
        raw["expected_document_version"] = float("nan")
    row.request_json = "{" if change == "truncated" else json.dumps(raw)
    with pytest.raises(MoneyError) as caught:
        verify(row)
    assert caught.value.code == "invoice_reservation_integrity"


@pytest.mark.parametrize("field", ["request_json", "snapshot_json"])
def test_utf8_bytes_not_character_count_define_saved_input_bounds(field):
    row, _ = saved()
    maximum = 65536 if field == "request_json" else 4259840
    setattr(row, field, "虚" * (maximum // 3 + 1))
    if field == "snapshot_json":
        row.snapshot_sha256 = hashlib.sha256(row.snapshot_json.encode()).hexdigest()
    with pytest.raises(MoneyError):
        verify(row)


def test_missing_or_non_reservation_object_fails_with_generic_storage_error():
    with pytest.raises(MoneyError) as caught:
        verify_invoice_reservation(None, owner_id=OWNER, ledger_id=OTHER)
    assert caught.value.code == "invoice_reservation_integrity"


@pytest.mark.parametrize("form", ["whitespace", "ordering", "duplicate"])
def test_stored_request_must_match_the_same_canonical_encoding_as_migration_0100(form):
    row, _ = saved(uppercase_request=True)
    original = row.request_json
    if form == "whitespace":
        row.request_json = " " + original
    elif form == "ordering":
        row.request_json = json.dumps(
            dict(reversed(list(json.loads(original).items()))), separators=(",", ":")
        )
    else:
        row.request_json = original[:-1] + ',"id":' + json.dumps(json.loads(original)["id"]) + "}"
    assert json.loads(row.request_json) == json.loads(original) and row.request_json != original
    with pytest.raises(MoneyError) as caught:
        verify(row)
    assert caught.value.code == "invoice_reservation_integrity"
