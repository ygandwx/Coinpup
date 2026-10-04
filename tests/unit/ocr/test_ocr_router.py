"""Browser task metadata uses existing authorization, validation and private error boundaries."""

import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import pytest
from coinpup_api.auth import AuthError, AuthService, Identity
from coinpup_api.config import Settings
from coinpup_api.ledger.service import LedgerError
from coinpup_api.main import create_app
from coinpup_api.ocr.contracts import JobResult, JobView
from coinpup_api.ocr.queue import OcrQueueService
from coinpup_api.security import csrf_token_for
from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError

OWNER = UUID("00000000-0000-4000-8000-000000000001")
LEDGER = UUID("00000000-0000-4000-8000-000000000002")
JOB = UUID("00000000-0000-4000-8000-000000000003")
FILE = UUID("00000000-0000-4000-8000-000000000004")
INTENT = UUID("00000000-0000-4000-8000-000000000005")
OTHER = UUID("00000000-0000-4000-8000-000000000099")
PREFIX = f"/api/v1/ledgers/{LEDGER}/ocr-jobs"
RETRY = f"{PREFIX}/{JOB}/retries"
TOKEN = "fictional-ocr-browser-session"
SECRET = "FICTIONAL-PRIVATE-INVOICE-DATA"
SQL = "SELECT fictional_private_path FROM private_storage"
ORIGIN = "http://localhost:8000"
HEADERS = {"origin": ORIGIN, "x-csrf-token": csrf_token_for(TOKEN)}
BODY = {"intent_id": str(INTENT), "file_id": str(FILE)}
WRITES = [(PREFIX, BODY, "create_job"), (RETRY, {"expected_version": 4}, "retry_job")]


class Probe:
    engine = None

    def check(self):
        pass

    def close(self):
        pass


def job_view():
    now = datetime(2026, 1, 1, tzinfo=UTC)
    return JobView(
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
        result=JobResult(summary={"pages": 1, "candidates": 1}, draft_ids=(OTHER,)),
    )


def forbid_service(*args, **kwargs):
    pytest.fail("Rejected browser request reached the task service")


def resolve(self, token):
    if token != TOKEN:
        raise AuthError("authentication_required", 401)
    return Identity(OWNER, "fictional-owner")


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(AuthService, "get_session", resolve)
    app = create_app(Settings(_env_file=None, environment="test"), Probe())
    with TestClient(app) as client:
        yield client


@pytest.fixture
def signed_in(client):
    client.cookies.set("coinpup_session", TOKEN)
    return client


@pytest.mark.parametrize("path,method", [(PREFIX, "list_jobs"), (f"{PREFIX}/{JOB}", "get_job")])
def test_private_task_reads_require_a_cookie_before_service(client, monkeypatch, path, method):
    monkeypatch.setattr(OcrQueueService, method, forbid_service)
    response = client.get(path)
    assert response.status_code == 401
    assert response.json() == {"detail": "authentication_required"}
    assert response.headers["cache-control"] == "no-store"
    assert str(FILE) not in response.text


@pytest.mark.parametrize("path,body,method", WRITES)
def test_writes_preserve_origin_then_session_then_csrf_precedence(
    signed_in, monkeypatch, path, body, method
):
    auth_calls = []

    def recorded_identity(self, token):
        auth_calls.append(token)
        return resolve(self, token)

    monkeypatch.setattr(AuthService, "get_session", recorded_identity)
    monkeypatch.setattr(OcrQueueService, method, forbid_service)
    for origin in (None, "null", "https://attacker.example"):
        headers = {"x-csrf-token": "wrong"}
        if origin is not None:
            headers["origin"] = origin
        response = signed_in.post(path, json=body, headers=headers)
        assert response.status_code == 403
        assert response.json() == {"detail": "origin_not_allowed"}
    assert auth_calls == []
    signed_in.cookies.clear()
    response = signed_in.post(path, json=body, headers={"origin": ORIGIN})
    assert response.status_code == 401
    assert response.json() == {"detail": "authentication_required"}
    assert auth_calls == [None]
    signed_in.cookies.set("coinpup_session", TOKEN)
    for csrf in (None, "wrong"):
        headers = {"origin": ORIGIN}
        if csrf is not None:
            headers["x-csrf-token"] = csrf
        response = signed_in.post(path, json=body, headers=headers)
        assert response.status_code == 403
        assert response.json() == {"detail": "csrf_failed"}


def test_revoked_cookie_cannot_read_task_or_retry_even_with_valid_csrf(signed_in, monkeypatch):
    def revoked(*args):
        raise AuthError("authentication_required", 401)

    monkeypatch.setattr(AuthService, "get_session", revoked)
    monkeypatch.setattr(OcrQueueService, "get_job", forbid_service)
    monkeypatch.setattr(OcrQueueService, "retry_job", forbid_service)
    assert signed_in.get(f"{PREFIX}/{JOB}").status_code == 401
    response = signed_in.post(RETRY, json={"expected_version": 4}, headers=HEADERS)
    assert response.status_code == 401 and SECRET not in response.text


def test_all_four_routes_forward_cookie_owner_and_typed_inputs_and_filter_private_extras(
    signed_in, monkeypatch
):
    calls = []
    public = job_view().model_dump(mode="json")
    internal = public | {"lease_token": SECRET, "blob_key": SECRET, "configuration": SECRET}

    def create(self, owner, ledger, body, *, configuration):
        calls.append(("create", owner, ledger, body.intent_id, body.file_id, configuration))
        return internal

    def listing(self, owner, ledger, **options):
        calls.append(("list", owner, ledger, options))
        return [internal]

    def get(self, owner, ledger, job):
        calls.append(("get", owner, ledger, job))
        return internal

    def retry(self, owner, ledger, job, expected_version):
        calls.append(("retry", owner, ledger, job, expected_version))
        return internal

    for name, implementation in (
        ("create_job", create),
        ("list_jobs", listing),
        ("get_job", get),
        ("retry_job", retry),
    ):
        monkeypatch.setattr(OcrQueueService, name, implementation)
    responses = [
        signed_in.post(PREFIX, json=BODY, headers=HEADERS),
        signed_in.get(
            PREFIX,
            params={
                "intent_id": str(INTENT),
                "state": "failed",
                "limit": 2,
                "offset": 3,
                "owner_id": str(OTHER),
            },
        ),
        signed_in.get(f"{PREFIX}/{JOB}"),
        signed_in.post(RETRY, json={"expected_version": 4}, headers=HEADERS),
    ]
    assert [response.status_code for response in responses] == [201, 200, 200, 200]
    assert [response.json() for response in responses] == [public, [public], public, public]
    assert calls == [
        ("create", OWNER, LEDGER, INTENT, FILE, None),
        ("list", OWNER, LEDGER, {"intent_id": INTENT, "state": "failed", "limit": 2, "offset": 3}),
        ("get", OWNER, LEDGER, JOB),
        ("retry", OWNER, LEDGER, JOB, 4),
    ]
    for response in responses:
        assert response.headers["cache-control"] == "no-store"
        assert SECRET not in response.text
    calls.clear()
    assert signed_in.get(PREFIX).status_code == 200
    assert calls == [
        ("list", OWNER, LEDGER, {"intent_id": None, "state": None, "limit": 100, "offset": 0})
    ]


def test_factory_copies_nested_configuration_without_prevalidating_old_intent(monkeypatch):
    configuration = {"future": {"labels": ["original"]}}
    app = create_app(
        Settings(_env_file=None, environment="test"), Probe(), ocr_configuration=configuration
    )
    configuration["future"]["labels"].append("mutated-after-app-creation")
    configuration["new"] = SECRET
    calls = []

    def replay(self, owner, ledger, body, *, configuration):
        calls.append(configuration)
        return job_view()

    monkeypatch.setattr(AuthService, "get_session", resolve)
    monkeypatch.setattr(OcrQueueService, "create_job", replay)
    with TestClient(app) as client:
        client.cookies.set("coinpup_session", TOKEN)
        response = client.post(PREFIX, json=BODY, headers=HEADERS)
    assert response.status_code == 201
    assert calls == [{"future": {"labels": ["original"]}}]


@pytest.mark.parametrize(
    "code,status,expected_status",
    [
        ("ocr_invalid_configuration", 422, 503),
        ("ocr_manifest_conflict", 409, 409),
        ("not_found", 404, 404),
        ("file_archived", 409, 409),
    ],
)
def test_absent_default_configuration_only_maps_the_core_configuration_error(
    signed_in, monkeypatch, code, status, expected_status
):
    def reject(*args, **kwargs):
        assert kwargs["configuration"] is None
        raise LedgerError(code, status, "Synthetic stable domain message")

    monkeypatch.setattr(OcrQueueService, "create_job", reject)
    response = signed_in.post(PREFIX, json=BODY, headers=HEADERS)
    assert response.status_code == expected_status
    expected = (
        {"code": "ocr_unavailable", "message": "OCR is unavailable."}
        if code == "ocr_invalid_configuration"
        else {"code": code, "message": "Synthetic stable domain message"}
    )
    assert response.json() == {"detail": expected}


def test_configured_invalid_settings_keep_the_core_422_and_retry_errors_are_not_remapped(
    monkeypatch,
):
    def reject(*args, **kwargs):
        raise LedgerError("ocr_invalid_configuration", 422, "OCR configuration is invalid.")

    monkeypatch.setattr(AuthService, "get_session", resolve)
    monkeypatch.setattr(OcrQueueService, "create_job", reject)
    monkeypatch.setattr(OcrQueueService, "retry_job", reject)
    configured = create_app(
        Settings(_env_file=None, environment="test"), Probe(), ocr_configuration={}
    )
    for app, path, body in (
        (configured, PREFIX, BODY),
        (
            create_app(Settings(_env_file=None, environment="test"), Probe()),
            RETRY,
            {"expected_version": 4},
        ),
    ):
        with TestClient(app) as client:
            client.cookies.set("coinpup_session", TOKEN)
            response = client.post(path, json=body, headers=HEADERS)
        assert response.status_code == 422
        assert response.json() == {
            "detail": {
                "code": "ocr_invalid_configuration",
                "message": "OCR configuration is invalid.",
            }
        }


@pytest.mark.parametrize("expected_version", [True, 1.0, "1", 0])
def test_retry_version_rejects_coercion_before_service(signed_in, monkeypatch, expected_version):
    monkeypatch.setattr(OcrQueueService, "retry_job", forbid_service)
    response = signed_in.post(RETRY, json={"expected_version": expected_version}, headers=HEADERS)
    assert response.status_code == 422
    assert response.json()["detail"] == [
        {
            "loc": ["body", "expected_version"],
            "type": "greater_than_equal" if expected_version == 0 else "int_type",
            "msg": "Invalid value",
        }
    ]


@pytest.mark.parametrize(
    "method,path,body,location",
    [
        ("GET", f"/api/v1/ledgers/{SECRET}/ocr-jobs", None, ["path", "ledger_id"]),
        ("POST", PREFIX, {"intent_id": str(INTENT), "file_id": SECRET}, ["body", "file_id"]),
        ("POST", PREFIX, BODY | {"owner_id": SECRET}, ["body", "owner_id"]),
        ("POST", RETRY, {"expected_version": 4, "lease_token": SECRET}, ["body", "lease_token"]),
    ],
)
def test_uuid_and_unknown_payload_validation_uses_global_private_error_handler(
    signed_in, monkeypatch, method, path, body, location
):
    for name in ("create_job", "list_jobs", "get_job", "retry_job"):
        monkeypatch.setattr(OcrQueueService, name, forbid_service)
    response = signed_in.request(method, path, json=body, headers=HEADERS)
    assert response.status_code == 422 and SECRET not in response.text
    error = response.json()["detail"][0]
    assert error["loc"] == location
    assert set(error) == {"loc", "type", "msg"} and error["msg"] == "Invalid value"


def test_invalid_list_filters_and_page_bounds_never_reach_service(signed_in, monkeypatch):
    monkeypatch.setattr(OcrQueueService, "list_jobs", forbid_service)
    for query in (
        {"limit": 0},
        {"limit": 201},
        {"limit": "1.5"},
        {"offset": -1},
        {"offset": 100001},
        {"state": SECRET},
        {"intent_id": SECRET},
    ):
        response = signed_in.get(PREFIX, params=query)
        assert response.status_code == 422 and SECRET not in response.text
        assert set(response.json()["detail"][0]) == {"loc", "type", "msg"}


def test_auth_and_task_database_errors_never_log_or_return_sql_paths_or_private_values(
    signed_in, monkeypatch, caplog
):
    def unavailable(*args, **kwargs):
        raise OperationalError(SQL, {"private": SECRET}, Exception("/private/" + SECRET))

    monkeypatch.setattr(OcrQueueService, "get_job", unavailable)
    response = signed_in.get(f"{PREFIX}/{JOB}")
    assert response.status_code == 503
    assert response.json() == {
        "detail": {"code": "ocr_unavailable", "message": "OCR is unavailable."}
    }
    assert SQL not in response.text + caplog.text and SECRET not in response.text + caplog.text
    monkeypatch.setattr(AuthService, "get_session", unavailable)
    response = signed_in.get(f"{PREFIX}/{JOB}")
    assert response.status_code == 503
    assert response.json() == {"detail": "authentication_unavailable"}
    assert SQL not in response.text + caplog.text and SECRET not in response.text + caplog.text


def test_fresh_default_api_startup_and_openapi_do_not_import_pdf_or_engine_dependencies():
    source = Path(__file__).resolve().parents[3] / "services/api/src"
    script = f"""
import importlib.abc
import sys
sys.path.insert(0, {str(source)!r})
class NoProcessingImports(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        forbidden = {{'pypdf', 'pdfplumber', 'pypdfium2', 'PIL', 'paddle', 'paddleocr'}}
        if fullname.split('.')[0] in forbidden:
            raise AssertionError('Default API imported a processing dependency: ' + fullname)
sys.meta_path.insert(0, NoProcessingImports())
from coinpup_api.config import Settings
from coinpup_api.main import create_app
class Probe:
    engine = None
    def check(self): pass
    def close(self): pass
app = create_app(Settings(_env_file=None, environment='test'), Probe())
assert '/api/v1/ledgers/{{ledger_id}}/ocr-jobs' in app.openapi()['paths']
"""
    result = subprocess.run(
        [sys.executable, "-B", "-c", script],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
