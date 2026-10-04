"""Engine-independent queue boundaries; all document text and identifiers are fictional."""

import hashlib
import json
from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal, localcontext
from types import SimpleNamespace
from uuid import UUID

import pytest
from coinpup_api.ledger.service import LedgerError
from coinpup_api.ocr.contracts import (
    CANDIDATE_BYTES,
    CONFIG_BYTES,
    RESULT_BYTES,
    Candidate,
    Completion,
    JobCreate,
    Lease,
    canonical_json,
    fence_hash,
    manifest_hash,
    prepare_completion,
    prepare_configuration,
)
from coinpup_api.ocr.queue import OcrQueueService
from pydantic import ValidationError

OWNER = UUID("00000000-0000-4000-8000-000000000001")
LEDGER = UUID("00000000-0000-4000-8000-000000000002")
JOB = UUID("00000000-0000-4000-8000-000000000003")
FILE = UUID("00000000-0000-4000-8000-000000000004")
INTENT = UUID("00000000-0000-4000-8000-000000000005")
TOKEN = UUID("00000000-0000-4000-8000-000000000006")
OTHER = UUID("00000000-0000-4000-8000-000000000099")
SECRET = "FICTIONAL-PRIVATE-DOCUMENT-TEXT"


def configuration(**updates):
    value = {"lease_seconds": 30, "retry_seconds": 5, "processing": {}}
    value.update(updates)
    return value


def candidate(source_key="page:1", **updates):
    value = {"source_key": source_key, "recognized": {}, "evidence": {}, "fields": {}}
    value.update(updates)
    return Candidate.model_validate(value)


def lease():
    return Lease(
        owner_id=OWNER,
        ledger_id=LEDGER,
        job_id=JOB,
        file_id=FILE,
        generation=1,
        token=TOKEN,
        lease_until=datetime(2026, 1, 1, tzinfo=UTC),
        configuration_json='{"private":"' + SECRET + '"}',
        blob_key="private/" + SECRET,
        sha256="a" * 64,
        byte_size=1024,
        media_type="application/pdf",
    )


def assert_invalid(call, code="ocr_invalid_payload"):
    with pytest.raises(LedgerError) as caught:
        call()
    assert caught.value.code == code
    assert caught.value.status == 422
    assert SECRET not in str(caught.value)
    assert "SELECT" not in str(caught.value)
    return caught.value


def test_published_byte_budgets_match_the_frozen_database_bounds():
    assert (CONFIG_BYTES, CANDIDATE_BYTES, RESULT_BYTES) == (16_384, 262_144, 1_048_576)


@pytest.mark.parametrize(
    ("value", "postgres_text"),
    [
        ({"text": "中文"}, '{"text": "中文"}'),
        ({"text": '\n\t"\\'}, '{"text": "\\n\\t\\"\\\\"}'),
        ({"value": 1e20}, '{"value": 100000000000000000000}'),
        ({"value": 1e-7}, '{"value": 0.0000001}'),
        ({"value": [True, None, 7]}, '{"value": [true, null, 7]}'),
    ],
)
def test_budget_uses_postgresql_text_bytes_not_compact_json(value, postgres_text):
    budget = len(postgres_text.encode("utf-8"))
    assert json.loads(canonical_json(value, budget)) == value
    assert_invalid(lambda: canonical_json(value, budget - 1))


def test_float_size_is_independent_of_decimal_arithmetic_context():
    with localcontext() as context:
        context.prec = 1
        encoded = canonical_json({"confidence": 0.987654321, "position": 1e20}, 100)
    assert json.loads(encoded) == {"confidence": 0.987654321, "position": 1e20}


@pytest.mark.parametrize(
    "bad",
    [
        {"private": SECRET, "bad": float("nan")},
        {"private": SECRET, "bad": float("inf")},
        {"private": SECRET, "bad": -float("inf")},
        {"private": SECRET, "bad": Decimal("12.30")},
        {"private": SECRET, "bad": {1, 2}},
        {"private": SECRET, "bad": (1, 2)},
        {"private": SECRET, "bad": b"binary"},
        {"private": SECRET, "bad": UUID(int=1)},
        {1: SECRET},
        {"private": SECRET + "\x00"},
        {"private\x00": SECRET},
        {"private": SECRET + "\ud800"},
    ],
)
def test_invalid_nested_json_is_rejected_with_stable_private_error(bad):
    error = assert_invalid(lambda: canonical_json(bad, RESULT_BYTES))
    assert str(error) == "OCR data is invalid or exceeds its limits."


def test_cyclic_payload_has_a_stable_error_and_cannot_escape_validation():
    cyclic = {"private": SECRET}
    cyclic["cycle"] = cyclic
    assert_invalid(lambda: canonical_json(cyclic, RESULT_BYTES))


@pytest.mark.parametrize(
    "changes",
    [
        {"lease_seconds": True},
        {"lease_seconds": "30"},
        {"lease_seconds": 30.0},
        {"lease_seconds": 0},
        {"lease_seconds": 3601},
        {"retry_seconds": False},
        {"retry_seconds": "5"},
        {"retry_seconds": -1},
        {"retry_seconds": 3601},
        {"processing": []},
        {"processing": None},
        {"unknown": SECRET},
    ],
)
def test_configuration_rejects_coercion_out_of_range_and_unknown_keys(changes):
    assert_invalid(
        lambda: prepare_configuration(configuration(**changes)), "ocr_invalid_configuration"
    )


@pytest.mark.parametrize("missing", ["lease_seconds", "retry_seconds", "processing"])
def test_configuration_requires_every_explicit_setting(missing):
    value = configuration()
    del value[missing]
    assert_invalid(lambda: prepare_configuration(value), "ocr_invalid_configuration")


@pytest.mark.parametrize("lease_seconds,retry_seconds", [(1, 0), (3600, 3600)])
def test_configuration_accepts_exact_integer_bounds(lease_seconds, retry_seconds):
    value = configuration(lease_seconds=lease_seconds, retry_seconds=retry_seconds)
    prepared, digest = prepare_configuration(value)
    assert prepared == value
    assert len(digest) == 64


def test_configuration_uses_utf8_budget_and_takes_a_detached_snapshot():
    overhead = len(b'{"lease_seconds": 30, "retry_seconds": 5, "processing": {"label": ""}}')
    label = "中" * ((CONFIG_BYTES - overhead) // 3)
    value = configuration(processing={"label": label})
    prepared, digest = prepare_configuration(value)
    value["processing"]["label"] = "changed"
    assert prepared["processing"]["label"] == label
    assert prepare_configuration(prepared)[1] == digest
    assert_invalid(
        lambda: prepare_configuration(configuration(processing={"label": label + "中"})),
        "ocr_invalid_configuration",
    )


def test_configuration_hash_preserves_json_meaning_and_explicit_value_spelling():
    first = configuration(processing={"amount": "12.30", "layout": [1, 2], "flag": None})
    reordered = {
        "processing": {"flag": None, "layout": [1, 2], "amount": "12.30"},
        "retry_seconds": 5,
        "lease_seconds": 30,
    }
    assert prepare_configuration(first)[1] == prepare_configuration(reordered)[1]
    for processing in [
        {"amount": "12.3", "layout": [1, 2], "flag": None},
        {"amount": "12.30", "layout": [2, 1], "flag": None},
        {"amount": "12.30", "layout": [1, 2]},
        {"amount": "12.30", "layout": [1, 2], "flag": []},
    ]:
        assert (
            prepare_configuration(first)[1]
            != prepare_configuration(configuration(processing=processing))[1]
        )


@pytest.mark.parametrize("name", ["recognized", "evidence", "fields"])
def test_each_candidate_object_has_its_own_postgresql_byte_budget(name):
    # This literal is the independent PostgreSQL jsonb::text representation of the object.
    overhead = len(b'{"text": ""}')
    text = "x" * (CANDIDATE_BYTES - overhead)
    accepted = Completion(candidates=(candidate(**{name: {"text": text}}),))
    prepared, _ = prepare_completion(accepted)
    assert prepared["candidates"][0][name]["text"] == text
    rejected = Completion(candidates=(candidate(**{name: {"text": text + "x"}}),))
    assert_invalid(lambda: prepare_completion(rejected))


def test_completion_enforces_aggregate_budget_even_with_valid_individual_objects():
    chunk = {"text": "x" * 200_000}
    completion = Completion(
        candidates=tuple(
            candidate(f"page:{page}", recognized=chunk, evidence=chunk, fields=chunk)
            for page in (1, 2)
        )
    )
    for item in completion.candidates:
        for value in (item.recognized, item.evidence, item.fields):
            assert canonical_json(value, CANDIDATE_BYTES)
    assert_invalid(lambda: prepare_completion(completion))


def test_success_envelope_is_reserved_before_transaction_even_without_candidates():
    completion = Completion(summary={"text": "x" * (RESULT_BYTES - 100)})
    raw = {"summary": completion.summary, "candidates": []}
    assert canonical_json(raw, RESULT_BYTES)
    assert_invalid(lambda: prepare_completion(completion))


def test_candidate_count_bound_and_duplicate_source_keys_do_not_silently_drop_results():
    at_limit = Completion(candidates=tuple(candidate(f"page:{i}") for i in range(200)))
    prepared, _ = prepare_completion(at_limit)
    assert len(prepared["candidates"]) == 200
    with pytest.raises(ValidationError):
        Completion(candidates=tuple(candidate(f"page:{i}") for i in range(201)))
    duplicate = Completion(candidates=(candidate(), candidate(fields={"text": SECRET})))
    assert_invalid(lambda: prepare_completion(duplicate))


@pytest.mark.parametrize("source_key", ["", " \t ", "page\x00:1", "page\n:1", "x" * 201, 12])
def test_source_key_cannot_be_empty_coerced_or_contain_controls(source_key):
    with pytest.raises(ValidationError):
        candidate(source_key)


@pytest.mark.parametrize("name", ["recognized", "evidence", "fields", "summary"])
def test_finish_rejects_non_object_recognition_evidence_fields_and_summary(name):
    with pytest.raises(ValidationError):
        if name == "summary":
            Completion.model_validate({"summary": []})
        else:
            candidate(**{name: []})


@pytest.mark.parametrize("extra", ["token", "fence_hash", "payload_hash", "draft_ids", "state"])
def test_unknown_completion_fields_cannot_supply_internal_finish_state(extra):
    with pytest.raises(ValidationError) as caught:
        Completion.model_validate({extra: SECRET})
    assert SECRET not in str(caught.value)


def test_unknown_candidate_and_job_fields_are_rejected_without_echoing_input():
    with pytest.raises(ValidationError) as caught:
        Candidate.model_validate(
            {
                "source_key": "page:1",
                "recognized": {},
                "evidence": {},
                "fields": {},
                "operation_id": SECRET,
            }
        )
    assert SECRET not in str(caught.value)
    with pytest.raises(ValidationError) as caught:
        JobCreate.model_validate({"intent_id": INTENT, "file_id": FILE, "result": SECRET})
    assert SECRET not in str(caught.value)


def test_completion_hash_preserves_order_spelling_and_private_summary_and_is_detached():
    first = Completion(
        summary={"z": 2, "a": 1},
        candidates=(candidate(fields={"amount": "12.30", "date": None}), candidate("page:2")),
    )
    equivalent = Completion(
        summary={"a": 1, "z": 2},
        candidates=(candidate(fields={"date": None, "amount": "12.30"}), candidate("page:2")),
    )
    prepared, digest = prepare_completion(first)
    assert prepare_completion(equivalent)[1] == digest
    alternatives = [
        Completion(summary=first.summary, candidates=tuple(reversed(first.candidates))),
        Completion(summary={"z": 3, "a": 1}, candidates=first.candidates),
        Completion(
            summary=first.summary,
            candidates=(candidate(fields={"amount": "12.3", "date": None}), candidate("page:2")),
        ),
        Completion(
            summary=first.summary,
            candidates=(candidate(fields={"amount": "12.30"}), candidate("page:2")),
        ),
        Completion(
            summary=first.summary,
            candidates=(candidate(fields={"amount": "12.30", "date": []}), candidate("page:2")),
        ),
    ]
    assert all(prepare_completion(value)[1] != digest for value in alternatives)
    first.candidates[0].fields["amount"] = "changed"
    first.summary["z"] = 100
    assert prepared["candidates"][0]["fields"]["amount"] == "12.30"
    assert prepared["summary"] == {"a": 1, "z": 2}


def test_nested_finish_values_still_receive_json_validation_after_model_parsing():
    completion = Completion(candidates=(candidate(evidence={"text": SECRET, "bad": float("nan")}),))
    assert_invalid(lambda: prepare_completion(completion))
    assert SECRET not in repr(completion)
    assert SECRET not in repr(completion.candidates[0])


@pytest.mark.parametrize("name", ["owner_id", "ledger_id", "intent_id", "file_id"])
def test_manifest_hash_binds_owner_ledger_intent_and_original_file(name):
    request = JobCreate(intent_id=INTENT, file_id=FILE)
    original = manifest_hash(OWNER, LEDGER, request)
    args = {"owner_id": OWNER, "ledger_id": LEDGER, "intent_id": INTENT, "file_id": FILE}
    args[name] = OTHER
    changed_request = JobCreate(intent_id=args["intent_id"], file_id=args["file_id"])
    assert manifest_hash(args["owner_id"], args["ledger_id"], changed_request) != original
    assert manifest_hash(OWNER, LEDGER, request) == original
    assert len(original) == 64


def test_v1_manifest_does_not_inherit_future_job_model_defaults():
    class FutureJobCreate(JobCreate):
        future_default: str = "not-part-of-the-v1-intent"

    request = JobCreate(intent_id=INTENT, file_id=FILE)
    future_request = FutureJobCreate(intent_id=INTENT, file_id=FILE)
    assert manifest_hash(OWNER, LEDGER, future_request) == manifest_hash(OWNER, LEDGER, request)


@pytest.mark.parametrize(
    "name", ["owner_id", "ledger_id", "job_id", "file_id", "generation", "token"]
)
def test_fence_hash_binds_every_lease_identity_and_secret_token(name):
    original = lease()
    replacement = 2 if name == "generation" else OTHER
    assert fence_hash(replace(original, **{name: replacement})) != fence_hash(original)


def test_renewed_lease_preserves_fence_but_rotation_changes_it_and_repr_hides_secrets():
    original = lease()
    renewed = replace(original, lease_until=original.lease_until + timedelta(seconds=30))
    assert fence_hash(renewed) == fence_hash(original)
    assert fence_hash(replace(original, token=OTHER, generation=2)) != fence_hash(original)
    assert str(TOKEN) not in repr(original)
    assert SECRET not in repr(original)
    with pytest.raises(FrozenInstanceError):
        original.generation = 2


def test_payload_digest_is_sha256_of_canonical_detached_payload():
    completion = Completion(summary={"pages": 1}, candidates=(candidate(),))
    prepared, digest = prepare_completion(completion)
    encoded = json.dumps(prepared, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    assert digest == hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def completed_job(summary=None):
    now = datetime(2026, 1, 1, tzinfo=UTC)
    return SimpleNamespace(
        id=JOB,
        ledger_id=LEDGER,
        file_id=FILE,
        intent_id=INTENT,
        state="succeeded",
        attempts=1,
        generation=1,
        version=3,
        retry_at=now,
        error_code=None,
        config_hash="a" * 64,
        created_at=now,
        updated_at=now,
        result={
            "format": 1,
            "fence_hash": "b" * 64,
            "payload_hash": "c" * 64,
            "summary": summary if summary is not None else {},
            "draft_ids": [str(OTHER)],
        },
    )


def test_public_result_exposes_only_bounded_counters_and_ids_not_internal_summary():
    job = completed_job(
        {
            "pages": 12,
            "manual_pages": 2,
            "candidates": 999,
            "text": SECRET,
            "exception": "SELECT private_path",
            "token": str(TOKEN),
        }
    )
    view = OcrQueueService._view(job)
    assert view.result.summary == {"pages": 12, "manual_pages": 2, "candidates": 1}
    assert view.result.draft_ids == (OTHER,)
    encoded = view.model_dump_json()
    for private in (SECRET, "SELECT", str(TOKEN), "fence_hash", "payload_hash", "exception"):
        assert private not in encoded
    assert job.result["summary"]["text"] == SECRET


@pytest.mark.parametrize("bad_counter", [True, -1, 1000001, "12", 1.5, None, {"text": SECRET}])
def test_public_summary_cannot_smuggle_text_through_a_counter(bad_counter):
    job = completed_job({"pages": bad_counter, "manual_pages": bad_counter})
    view = OcrQueueService._view(job)
    assert view.result.summary == {"candidates": 1}


@pytest.mark.parametrize(
    "updates",
    [
        {"format": True},
        {"format": 2},
        {"summary": SECRET},
        {"draft_ids": SECRET},
        {"draft_ids": [SECRET]},
    ],
)
def test_corrupt_stored_result_returns_stable_unavailable_error_without_raw_data(updates):
    job = completed_job()
    job.result.update(updates)
    with pytest.raises(LedgerError) as caught:
        OcrQueueService._view(job)
    assert caught.value.code == "ocr_result_unavailable"
    assert caught.value.status == 503
    assert str(caught.value) == "The OCR result is unavailable."
    assert SECRET not in str(caught.value)


@pytest.mark.parametrize(
    "bad_code", [SECRET, "SELECT private_path", "", "ocr_lease_lost", None, True, []]
)
def test_unknown_failure_code_is_rejected_before_any_database_transaction(monkeypatch, bad_code):
    service = object.__new__(OcrQueueService)

    def forbidden_transaction(*args, **kwargs):
        pytest.fail("Invalid failure payload entered a database transaction")

    monkeypatch.setattr(service, "_transaction", forbidden_transaction)
    assert_invalid(lambda: service.fail(lease(), bad_code))


@pytest.mark.parametrize("bad_retryable", [0, 1, "true", None, {}])
def test_failure_retry_flag_is_strict_bool_before_database_access(monkeypatch, bad_retryable):
    service = object.__new__(OcrQueueService)

    def forbidden_transaction(*args, **kwargs):
        pytest.fail("Invalid retry payload entered a database transaction")

    monkeypatch.setattr(service, "_transaction", forbidden_transaction)
    assert_invalid(lambda: service.fail(lease(), "processor_timeout", retryable=bad_retryable))


@pytest.mark.parametrize(
    "target,name,new_value",
    [
        ("job", "file_id", OTHER),
        ("job", "configuration", {"private": "changed"}),
        ("source", "blob_key", "other/private/blob"),
        ("source", "sha256", "b" * 64),
        ("source", "byte_size", 1025),
        ("source", "detected_media_type", "image/png"),
    ],
)
def test_lease_source_snapshot_cannot_be_replaced_before_a_worker_write(target, name, new_value):
    original = lease()
    job = SimpleNamespace(file_id=FILE, configuration=json.loads(original.configuration_json))
    source = SimpleNamespace(
        blob_key=original.blob_key,
        sha256=original.sha256,
        byte_size=original.byte_size,
        detected_media_type=original.media_type,
    )
    OcrQueueService._source_matches(original, job, source)
    setattr(job if target == "job" else source, name, new_value)
    with pytest.raises(LedgerError) as caught:
        OcrQueueService._source_matches(original, job, source)
    assert caught.value.code == "ocr_lease_lost"
    assert caught.value.status == 409
    assert SECRET not in str(caught.value)
