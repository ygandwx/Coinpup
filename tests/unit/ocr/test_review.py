"""Human review cannot authorize fields by changing editable OCR metadata."""

from copy import deepcopy
from types import SimpleNamespace

import pytest
from coinpup_api.ledger.service import LedgerError
from coinpup_api.ocr.candidates import completion_from_result
from coinpup_api.ocr.review import (
    DraftReviewService,
    DraftReviewUpdate,
    HumanReview,
    original_review,
    reviewed_fields,
)
from pydantic import ValidationError
from sqlalchemy.exc import OperationalError

from tests.unit.ocr.test_candidates import statement
from tests.unit.ocr.test_ocr_router import HEADERS, LEDGER, OTHER, OWNER, SQL, TOKEN, forbid_service
from tests.unit.ocr.test_ocr_router import client as client

PATH = f"/api/v1/ledgers/{LEDGER}/ocr-drafts/{OTHER}/review"


def evidence(*, ocr=(0,)):
    result = statement("10.00", ocr=ocr)
    candidate = completion_from_result(result).candidates[0]
    return (
        SimpleNamespace(recognized=candidate.recognized, fields=candidate.fields),
        SimpleNamespace(result={"summary": {"recognition": result}}),
    )


def test_editable_metadata_never_grants_ocr_prefill_or_bypasses_required_review():
    draft, job = evidence()
    draft.fields = {"review": [], "confirmed": ["forged"], "requires_confirmation": False}
    fields = original_review(draft, job)
    assert len(fields) == 3 and all(f.requires_confirmation for f in fields)
    assert all(f.source == "ocr" and f.suggested_value is None for f in fields)
    with pytest.raises(LedgerError) as error:
        reviewed_fields(draft, job, HumanReview(), complete=True)
    assert error.value.code == "ocr_review_required"
    selected = HumanReview(confirmed=[f.path for f in fields], entry={"amount": "12.30"})
    before = deepcopy(job.result)
    final = reviewed_fields(draft, job, selected, complete=True)
    assert final["entry"] == {"amount": "12.30"} and job.result == before
    assert final["confirmed"] == selected.confirmed


def test_text_prefill_is_preserved_and_unknown_confirmation_paths_rejected():
    draft, job = evidence(ocr=())
    fields = original_review(draft, job)
    assert all(not f.requires_confirmation and f.suggested_value for f in fields)
    reviewed_fields(draft, job, HumanReview(), complete=True)
    with pytest.raises(LedgerError) as error:
        reviewed_fields(draft, job, HumanReview(confirmed=["header.fictional"]))
    assert error.value.code == "ocr_invalid_review"


@pytest.mark.parametrize("change", ["missing_result", "missing_field", "duplicate_field"])
def test_inconsistent_original_evidence_fails_closed(change):
    draft, job = evidence()
    if change == "missing_result":
        job.result = {}
    elif change == "missing_field":
        draft.recognized["fields"][0]["path"] = "missing"
    else:
        draft.recognized["fields"].append(draft.recognized["fields"][0])
    with pytest.raises(LedgerError) as error:
        original_review(draft, job)
    assert error.value.code == "ocr_invalid_evidence"


@pytest.mark.parametrize(
    "review",
    [
        {"confirmed": ["a", "a"]},
        {"confirmed": [1]},
        {"confirmed": ["x"] * 1001},
        {"entry": []},
        {"automatic": True},
    ],
)
def test_review_input_rejects_ambiguous_or_unbounded_shape(review):
    with pytest.raises(ValidationError):
        HumanReview.model_validate(review)


@pytest.mark.parametrize("entry", [{"text": "x" * 262144}, {"bad": float("inf")}, {"bad": "\x00"}])
def test_partial_entry_still_obeys_private_json_bounds(entry):
    with pytest.raises(LedgerError):
        reviewed_fields(*evidence(), HumanReview(entry=entry))


def test_manual_legacy_draft_has_no_invented_recognition_fields():
    assert original_review(SimpleNamespace(recognized={}), SimpleNamespace(result={})) == []


def test_review_authentication_and_csrf_precede_storage(client, monkeypatch):
    monkeypatch.setattr(DraftReviewService, "get_review", forbid_service)
    monkeypatch.setattr(DraftReviewService, "update_review", forbid_service)
    assert client.get(PATH).status_code == 401
    client.cookies.set("coinpup_session", TOKEN)
    assert client.put(PATH, json={"expected_version": 1, "review": {}}).status_code == 403
    assert (
        client.put(
            PATH,
            headers={**HEADERS, "origin": "https://fictional.invalid"},
            json={"expected_version": 1, "review": {}},
        ).status_code
        == 403
    )


def test_authenticated_review_forwards_owner_version_and_original_partial_entry(
    client, monkeypatch
):
    client.cookies.set("coinpup_session", TOKEN)
    calls = []

    def save(self, owner, ledger, draft, payload):
        calls.append((owner, ledger, draft, payload))
        return {
            "draft_id": draft,
            "version": 2,
            "status": payload.status,
            "fields": [],
            "review": payload.review,
        }

    monkeypatch.setattr(DraftReviewService, "update_review", save)
    raw = {"expected_version": 1, "status": "ignored", "review": {"entry": {"amount": "1.00"}}}
    response = client.put(PATH, headers=HEADERS, json=raw)
    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    assert calls == [(OWNER, LEDGER, OTHER, DraftReviewUpdate.model_validate(raw))]
    assert response.json()["review"]["entry"]["amount"] == "1.00"


@pytest.mark.parametrize("method", ["get", "put"])
def test_review_errors_redact_originals_and_driver_details(client, monkeypatch, method):
    client.cookies.set("coinpup_session", TOKEN)

    def failure(*args):
        raise OperationalError(SQL, {}, RuntimeError("Fictional private receipt"))

    monkeypatch.setattr(
        DraftReviewService, "get_review" if method == "get" else "update_review", failure
    )
    response = client.request(
        method,
        PATH,
        headers=HEADERS,
        json={"expected_version": 1, "review": {}} if method == "put" else None,
    )
    assert response.status_code == 503 and response.json()["detail"]["code"] == "ocr_unavailable"
    assert "Fictional" not in response.text and SQL not in response.text
