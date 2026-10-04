"""Real sessions expose queue metadata; fictional callbacks do not demonstrate OCR."""

from copy import deepcopy
from uuid import uuid4

import pytest
from coinpup_api.config import Settings
from coinpup_api.main import create_app
from fastapi.testclient import TestClient

from tests.integration.ocr.test_ocr_queue import archive, completion, create, finances
from tests.integration.ocr.test_ocr_queue import queue_structure as queue_structure
from tests.integration.ocr.test_ocr_schema import ocr_structure as ocr_structure
from tests.integration.ocr.test_ocr_schema import rows

pytestmark = pytest.mark.integration


@pytest.fixture
def api(queue_structure, authenticated_client):
    s = queue_structure
    client, engine, owner = authenticated_client
    assert engine is s["engine"] and owner == s["owner"]
    s["client"] = client
    return s


def path(s, ledger=0):
    return f"/api/v1/ledgers/{s['ledgers'][ledger]}/ocr-jobs"


def body(s, *, intent=None, ledger=0):
    return {"intent_id": str(intent or uuid4()), "file_id": str(s["files"][ledger])}


def snapshot(s):
    return rows(s["engine"]), finances(s["engine"])


def rejected(s, status, code, request):
    before = snapshot(s)
    response = request()
    assert response.status_code == status
    detail = response.json()["detail"]
    assert (detail["code"] if isinstance(detail, dict) else detail) == code
    assert response.headers["cache-control"] == "no-store"
    assert snapshot(s) == before


def test_default_unconfigured_creation_returns_503_without_allocating_intent(api):
    s = api
    request = body(s)
    rejected(s, 503, "ocr_unavailable", lambda: s["client"].post(path(s), json=request))
    # An unavailable attempt has no committed intent; recovery is a real scoped query.
    result = s["client"].get(path(s), params={"intent_id": request["intent_id"]})
    assert result.status_code == 200 and result.json() == []


def test_existing_intent_replays_current_result_and_remains_readable_after_archive(api):
    s = api
    created = create(s)
    lease = s["queue"].claim(s["owner"])
    callback = completion().model_copy(
        update={
            "summary": {
                "pages": 1,
                "manual_pages": 0,
                "private_note": "Fictional internal-only callback metadata",
            }
        }
    )
    finished = s["queue"].finish(lease, callback)
    archive(s, "file")
    before = snapshot(s)
    expected = finished.model_dump(mode="json")
    response = s["client"].post(path(s), json=body(s, intent=created.intent_id))
    assert response.status_code == 201 and response.json() == expected
    item = s["client"].get(f"{path(s)}/{created.id}")
    listed = s["client"].get(
        path(s),
        params={
            "intent_id": str(created.intent_id),
            "state": "succeeded",
            "limit": 1,
            "offset": 0,
        },
    )
    assert item.status_code == listed.status_code == 200
    assert item.json() == expected and listed.json() == [expected]
    assert expected["result"]["summary"] == {"pages": 1, "manual_pages": 0, "candidates": 1}
    assert str(lease.token) not in item.text and lease.blob_key not in item.text
    assert "private_note" not in item.text
    assert (
        not {"lease_token", "lease_until", "configuration", "recognized", "evidence"}
        & item.json().keys()
    )
    assert snapshot(s) == before
    rejected(
        s,
        409,
        "ocr_manifest_conflict",
        lambda: s["client"].post(path(s), json=body(s, intent=created.intent_id, ledger=1)),
    )


def test_cross_scope_and_missing_records_stay_404_before_availability_check(api):
    s = api
    created = create(s)
    for request in (
        lambda: s["client"].post(path(s), json=body(s, ledger=1)),
        lambda: s["client"].post(path(s, 1), json=body(s, intent=created.intent_id, ledger=1)),
        lambda: s["client"].get(f"{path(s, 1)}/{created.id}"),
        lambda: s["client"].get(f"{path(s)}/{uuid4()}"),
        lambda: s["client"].post(f"/api/v1/ledgers/{uuid4()}/ocr-jobs", json=body(s)),
    ):
        rejected(s, 404, "not_found", request)


@pytest.mark.parametrize("target", ["file", "entity"])
def test_archived_sources_keep_their_error_before_unconfigured_503(api, target):
    s = api
    created = create(s)
    failed = s["queue"].fail(s["queue"].claim(s["owner"]), "invalid_document")
    archive(s, target)
    rejected(
        s,
        409,
        "file_archived" if target == "file" else "entity_archived",
        lambda: s["client"].post(path(s), json=body(s)),
    )
    rejected(
        s,
        409,
        "file_archived" if target == "file" else "entity_archived",
        lambda: s["client"].post(
            f"{path(s)}/{created.id}/retries", json={"expected_version": failed.version}
        ),
    )
    readable = s["client"].get(f"{path(s)}/{created.id}")
    assert readable.status_code == 200 and readable.json()["state"] == "failed"


def test_paused_retry_uses_original_configuration_and_strict_version_cas(api):
    s = api
    created = create(s)
    lease = s["queue"].claim(s["owner"])
    failed = s["queue"].fail(lease, "invalid_document")
    stored_configuration = rows(s["engine"])["ocr_jobs"][0]["configuration"]
    financial = finances(s["engine"])
    retry_path = f"{path(s)}/{created.id}/retries"
    retried = s["client"].post(retry_path, json={"expected_version": failed.version})
    assert retried.status_code == 200
    pending = retried.json()
    assert pending["state"] == "pending" and pending["version"] == failed.version + 1
    assert pending["attempts"] == failed.attempts and pending["generation"] == failed.generation
    assert pending["config_hash"] == failed.config_hash
    assert rows(s["engine"])["ocr_jobs"][0]["configuration"] == stored_configuration
    recovered = s["client"].get(path(s), params={"intent_id": str(created.intent_id)})
    assert recovered.status_code == 200 and recovered.json() == [pending]
    rejected(
        s,
        409,
        "version_conflict",
        lambda: s["client"].post(retry_path, json={"expected_version": failed.version}),
    )
    rejected(
        s,
        409,
        "ocr_not_retryable",
        lambda: s["client"].post(retry_path, json={"expected_version": pending["version"]}),
    )
    assert finances(s["engine"]) == financial


def test_http_manual_retries_stop_at_three_total_attempts(api):
    s = api
    created = create(s)
    financial = finances(s["engine"])
    retry_path = f"{path(s)}/{created.id}/retries"
    for attempt in range(1, 4):
        lease = s["queue"].claim(s["owner"])
        failed = s["queue"].fail(lease, "processing_failed")
        assert failed.attempts == attempt
        if attempt < 3:
            response = s["client"].post(retry_path, json={"expected_version": failed.version})
            assert response.status_code == 200 and response.json()["state"] == "pending"
        else:
            rejected(
                s,
                409,
                "ocr_retry_exhausted",
                lambda version=failed.version: s["client"].post(
                    retry_path, json={"expected_version": version}
                ),
            )
    assert finances(s["engine"]) == financial


def test_cookie_origin_and_csrf_reject_before_queue_availability(api):
    s = api
    client, request = s["client"], body(s)
    cookie = client.cookies.get("coinpup_session")
    client.cookies.clear()
    try:
        rejected(s, 401, "authentication_required", lambda: client.post(path(s), json=request))
        rejected(s, 401, "authentication_required", lambda: client.get(path(s)))
    finally:
        client.cookies.set("coinpup_session", cookie)
    rejected(
        s,
        403,
        "origin_not_allowed",
        lambda: client.post(path(s), json=request, headers={"origin": "https://fictional.invalid"}),
    )
    csrf = client.headers.pop("x-csrf-token")
    try:
        rejected(s, 403, "csrf_failed", lambda: client.post(path(s), json=request))
    finally:
        client.headers["x-csrf-token"] = csrf


def test_request_shapes_and_filters_are_validated_without_writes(api):
    s = api
    created = create(s)
    failed = s["queue"].fail(s["queue"].claim(s["owner"]), "invalid_document")
    before = snapshot(s)
    for request in (
        body(s) | {"amount": "1.00"},
        body(s) | {"intent_id": "not-a-uuid"},
    ):
        response = s["client"].post(path(s), json=request)
        assert response.status_code == 422
    for version in (True, 1.0, "1", 0, None):
        response = s["client"].post(
            f"{path(s)}/{created.id}/retries", json={"expected_version": version}
        )
        assert response.status_code == 422
    response = s["client"].post(
        f"{path(s)}/{created.id}/retries",
        json={
            "expected_version": failed.version,
            "amount": "1.00",
        },
    )
    assert response.status_code == 422
    for query in ({"state": "broken"}, {"limit": 0}, {"limit": 201}, {"offset": -1}):
        response = s["client"].get(path(s), params=query)
        assert response.status_code == 422
    assert snapshot(s) == before


def test_explicit_test_profile_allocates_pending_job_through_real_factory(api):
    s = api

    class Probe:
        engine = s["engine"]

        def check(self):
            pass

        def close(self):
            pass  # The shared disposable fixture owns this real engine.

    # This profile is test-only queue metadata, not a selected or executed engine.
    settings = Settings(_env_file=None, environment="test")
    financial = finances(s["engine"])
    profile = deepcopy(s["configuration"])
    app = create_app(settings, Probe(), ocr_configuration=profile)
    profile["processing"]["revision"] = 2
    with TestClient(app) as configured:
        configured.cookies.update(s["client"].cookies)
        configured.headers.update(s["client"].headers)
        request = body(s)
        response = configured.post(path(s), json=request)
        assert response.status_code == 201 and response.json()["state"] == "pending"
        assert response.json()["result"] is None and response.json()["attempts"] == 0
        replay = configured.post(path(s), json=request)
        assert replay.status_code == 201 and replay.json() == response.json()
    assert len(rows(s["engine"])["ocr_jobs"]) == 1 and finances(s["engine"]) == financial
    assert rows(s["engine"])["ocr_jobs"][0]["configuration"] == s["configuration"]
