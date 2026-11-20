"""No false success from one PDF, changed numbering, another ledger or a new intent."""

from copy import deepcopy
from uuid import uuid4

import pytest
from coinpup_api.business.invoice_issuance_results import (
    InvoiceIssuanceComplete,
    InvoiceIssuanceIdentity,
    InvoiceIssuancePending,
    InvoiceIssuanceResult,
    verify_issuance_result_identity,
)
from coinpup_api.business.invoice_numbering import MAX_SEQUENCE
from pydantic import TypeAdapter, ValidationError

RESULT = TypeAdapter(InvoiceIssuanceResult)


def reservation():
    return dict(
        kind="invoice_issuance",
        id=str(uuid4()),
        ledger_id=str(uuid4()),
        document_id=str(uuid4()),
        document_version=9,
        configuration_id=str(uuid4()),
        configuration_version=3,
        previous_issuance_id=str(uuid4()),
        numbering={"prefix": "虚构-", "minimum_digits": 6},
        sequence="1",
        number="虚构-000001",
        snapshot_sha256="a" * 64,
        template_version="commercial-v1",
    )


def artifacts():
    return [
        dict(
            language=language,
            file_id=str(uuid4()),
            sha256=character * 64,
            byte_size=size,
            media_type="application/pdf",
        )
        for language, character, size in [("zh", "b", 12345), ("en", "c", 10234)]
    ]


def test_pending_and_complete_have_distinct_exact_round_trips():
    identity = reservation()
    pending = identity | dict(state="pending", artifacts=[])
    complete = identity | dict(state="complete", artifacts=artifacts())
    saved = InvoiceIssuanceIdentity.model_validate(identity)
    for body, cls in [(pending, InvoiceIssuancePending), (complete, InvoiceIssuanceComplete)]:
        result = verify_issuance_result_identity(body, saved)
        assert isinstance(result, cls)
        assert result.model_dump(mode="json") == body
        assert RESULT.validate_json(result.model_dump_json()) == result
    # A newly configured prefix cannot change the old reservation or its result.
    changed = saved.model_copy(update={"number": "NEW-000001"})
    with pytest.raises(ValidationError):
        verify_issuance_result_identity(complete, changed)


@pytest.mark.parametrize("state", ["pending", "complete"])
@pytest.mark.parametrize("count", [1, 3])
def test_one_or_extra_artifacts_cannot_mean_success(state, count):
    files = (artifacts() * 2)[:count]
    with pytest.raises(ValidationError):
        RESULT.validate_python(reservation() | dict(state=state, artifacts=files))


def test_no_files_cannot_be_complete_and_two_files_cannot_be_pending():
    for state, files in [("complete", []), ("pending", artifacts())]:
        with pytest.raises(ValidationError):
            RESULT.validate_python(reservation() | dict(state=state, artifacts=files))


@pytest.mark.parametrize("defect", ["order", "language", "file_identity", "missing"])
def test_complete_pair_cannot_be_reordered_or_replaced_by_duplicate_file(defect):
    files = artifacts()
    if defect == "order":
        files.reverse()
    elif defect == "language":
        files[1]["language"] = "zh"
    elif defect == "file_identity":
        files[1]["file_id"] = files[0]["file_id"]
    else:
        del files[1]["sha256"]
    with pytest.raises(ValidationError):
        RESULT.validate_python(reservation() | dict(state="complete", artifacts=files))


@pytest.mark.parametrize(
    "field,value",
    [
        ("byte_size", 0),
        ("byte_size", -1),
        ("byte_size", True),
        ("byte_size", "123"),
        ("byte_size", 1.5),
        ("byte_size", 2**63),
        ("sha256", "B" * 64),
        ("sha256", "b" * 63),
        ("sha256", "b" * 64 + "\n"),
        ("media_type", "image/png"),
        ("language", "fr"),
        ("file_id", "not-a-uuid"),
        ("path", "/private/fictional.pdf"),
        ("url", "https://invalid.example/fictional.pdf"),
    ],
)
def test_artifact_identity_does_not_coerce_or_accept_incomplete_or_external_files(field, value):
    files = artifacts()
    files[0][field] = value
    with pytest.raises(ValidationError):
        RESULT.validate_python(reservation() | dict(state="complete", artifacts=files))


@pytest.mark.parametrize("field", list(reservation()))
def test_every_reserved_identity_field_is_required(field):
    body = reservation() | dict(state="pending", artifacts=[])
    del body[field]
    with pytest.raises(ValidationError):
        RESULT.validate_python(body)


@pytest.mark.parametrize(
    "field,value",
    [
        ("id", str(uuid4())),
        ("ledger_id", str(uuid4())),
        ("document_id", str(uuid4())),
        ("document_version", 10),
        ("configuration_id", str(uuid4())),
        ("configuration_version", 4),
        ("previous_issuance_id", None),
        ("snapshot_sha256", "d" * 64),
        ("template_version", "commercial-v2"),
    ],
)
@pytest.mark.parametrize("state", ["pending", "complete"])
def test_result_matches_original_reservation_including_scope_version_and_previous(
    field, value, state
):
    identity = reservation()
    body = identity | {
        field: value,
        "state": state,
        "artifacts": artifacts() if state == "complete" else [],
    }
    # Each counterfeit is structurally valid, but is not the saved intent.
    RESULT.validate_python(body)
    with pytest.raises(ValueError, match="saved reservation"):
        verify_issuance_result_identity(body, InvoiceIssuanceIdentity.model_validate(identity))


def test_another_valid_number_cannot_be_substituted_for_the_reserved_number():
    identity = reservation()
    body = identity | dict(sequence="2", number="虚构-000002", state="pending", artifacts=[])
    RESULT.validate_python(body)
    with pytest.raises(ValueError, match="saved reservation"):
        verify_issuance_result_identity(body, InvoiceIssuanceIdentity.model_validate(identity))


@pytest.mark.parametrize(
    "change",
    [
        {"sequence": 1},
        {"sequence": "01"},
        {"sequence": "0"},
        {"sequence": str(MAX_SEQUENCE + 1)},
        {"number": "虚构-000002"},
        {"document_version": 1},
        {"document_version": True},
        {"configuration_version": "3"},
        {"configuration_version": 2**31},
        {"snapshot_sha256": "corrupt"},
        {"template_version": "https://invalid.example/template"},
        {"template_version": "v" * 65},
        {"template_version": "v1\n"},
        {"state": "ready"},
        {"response_status": 201},
    ],
)
def test_fixed_identity_is_strict_and_cannot_imply_financial_success(change):
    with pytest.raises(ValidationError):
        RESULT.validate_python(reservation() | dict(state="pending", artifacts=[]) | change)


def test_maximum_sequence_remains_exact_and_immutable_after_input_mutation():
    identity = reservation() | dict(sequence=str(MAX_SEQUENCE), number="虚构-" + str(MAX_SEQUENCE))
    saved = InvoiceIssuanceIdentity.model_validate(identity)
    body = identity | dict(state="complete", artifacts=artifacts())
    original = deepcopy(body)
    result = verify_issuance_result_identity(body, saved)
    body["artifacts"][0]["byte_size"] = 999
    body["numbering"]["prefix"] = "CHANGED-"
    assert result.model_dump(mode="json") == original
    with pytest.raises(ValidationError):
        result.artifacts[0].byte_size = 999
    with pytest.raises(ValidationError):
        result.numbering.prefix = "CHANGED-"
    bypassed = result.model_copy(update={"artifacts": result.artifacts[:1]})
    with pytest.raises(ValidationError):
        verify_issuance_result_identity(bypassed, saved)


def test_self_predecessor_is_rejected():
    body = reservation()
    with pytest.raises(ValidationError):
        RESULT.validate_python(
            body | dict(previous_issuance_id=body["id"], state="pending", artifacts=[])
        )
