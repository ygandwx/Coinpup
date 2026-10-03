"""Uploads must authenticate before consuming bytes and never expose storage internals."""

import hashlib
import io
from datetime import UTC, datetime
from uuid import UUID

import pytest
from coinpup_api.auth import AuthError, AuthService, Identity
from coinpup_api.config import Settings
from coinpup_api.documents.router import create_document_router
from coinpup_api.documents.schemas import FileResponse, UploadCompletion, UploadResponse
from coinpup_api.documents.service import DocumentService
from coinpup_api.documents.storage import FileStoreError, StagedFile
from coinpup_api.ledger.service import LedgerError
from coinpup_api.security import csrf_token_for
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError
from starlette.requests import ClientDisconnect

OWNER = UUID("00000000-0000-4000-8000-000000000001")
LEDGER = UUID("00000000-0000-4000-8000-000000000002")
UPLOAD = UUID("00000000-0000-4000-8000-000000000003")
FILE = UUID("00000000-0000-4000-8000-000000000004")
PREFIX = f"/api/v1/ledgers/{LEDGER}"
CONTENT = PREFIX + f"/uploads/{UPLOAD}/content"
PDF = b"%PDF-1.7\nfictional unit fixture\n%%EOF\n"
TOKEN = "fictional-document-session"
HEADERS = {"origin": "http://localhost:8000", "x-csrf-token": csrf_token_for(TOKEN)}
NOW = datetime.now(UTC)


def upload_response():
    return UploadResponse(
        id=UPLOAD,
        ledger_id=LEDGER,
        original_filename="fictional.pdf",
        declared_size=len(PDF),
        operation_id=None,
        state="pending",
        created_at=NOW,
        completed_at=None,
        response=None,
    )


def completion():
    return UploadCompletion(
        upload_id=UPLOAD,
        file_id=FILE,
        link_id=None,
        duplicate=False,
        sha256=hashlib.sha256(PDF).hexdigest(),
        byte_size=len(PDF),
        media_type="application/pdf",
    )


class Store:
    def __init__(self):
        self.events = []
        self.handle = None

    async def stage(self, stream, size):
        self.events.append("stage")
        data = b"".join([chunk async for chunk in stream])
        assert data == PDF and size == len(PDF)
        return StagedFile("a" * 32, hashlib.sha256(data).hexdigest(), len(data), "application/pdf")

    def publish(self, staged):
        self.events.append("publish")
        return "b" * 32

    def discard(self, staged):
        self.events.append("discard")

    def open_blob(self, key, digest, size):
        self.events.append("open")
        assert (key, digest, size) == ("b" * 32, hashlib.sha256(PDF).hexdigest(), len(PDF))
        self.handle = io.BytesIO(PDF)
        return self.handle


@pytest.fixture
def boundary(monkeypatch):
    store = Store()
    app = FastAPI()
    app.include_router(
        create_document_router(Settings(_env_file=None, environment="test"), None, store)
    )

    def identity(self, token):
        if token != TOKEN:
            raise AuthError("authentication_required", 401)
        return Identity(OWNER, "fictional-owner")

    monkeypatch.setattr(AuthService, "get_session", identity)
    with TestClient(app) as client:
        client.cookies.set("coinpup_session", TOKEN)
        yield client, store


def wire(monkeypatch, store, publish=True):
    def preflight(self, owner, ledger, upload, *, for_upload):
        assert (owner, ledger, upload, for_upload) == (OWNER, LEDGER, UPLOAD, True)
        store.events.append("authorize")
        return upload_response()

    def check(self, owner, ledger, upload, staged):
        store.events.append("check")
        return publish

    def finish(self, owner, ledger, upload, staged, key):
        assert key == ("b" * 32 if publish else None)
        store.events.append("commit")
        return completion()

    monkeypatch.setattr(DocumentService, "get_upload", preflight)
    monkeypatch.setattr(DocumentService, "needs_publish", check)
    monkeypatch.setattr(
        DocumentService,
        "get_existing_blob",
        lambda *args: ("b" * 32, hashlib.sha256(PDF).hexdigest(), len(PDF)),
    )
    monkeypatch.setattr(DocumentService, "finalize_upload", finish)


@pytest.mark.parametrize("publish", [True, False])
def test_raw_upload_stream_and_ready_retry_both_consume_bytes(boundary, monkeypatch, publish):
    client, store = boundary
    wire(monkeypatch, store, publish)
    response = client.put(
        CONTENT, content=PDF, headers={**HEADERS, "content-type": "application/octet-stream"}
    )
    assert response.status_code == 200 and response.json() == completion().model_dump(mode="json")
    assert store.events == [
        "authorize",
        "stage",
        "check",
        "publish" if publish else "open",
        "commit",
        "discard",
    ]


@pytest.mark.parametrize(
    "headers", [{}, {"origin": "https://attacker.example"}, {"origin": "http://localhost:8000"}]
)
def test_origin_and_csrf_are_checked_before_any_upload_io(boundary, headers):
    client, store = boundary
    assert client.put(CONTENT, content=PDF, headers=headers).status_code == 403
    assert store.events == []


@pytest.mark.parametrize(
    "method,path,body",
    [
        (
            "POST",
            PREFIX + "/uploads",
            {"id": str(UPLOAD), "original_filename": "fixture.pdf", "declared_size": 1},
        ),
        ("PATCH", PREFIX + f"/files/{FILE}", {"expected_version": 1, "archived": True}),
        ("PUT", PREFIX + f"/operations/{FILE}/files/{FILE}", None),
        (
            "PATCH",
            PREFIX + f"/operations/{FILE}/files/{FILE}",
            {"expected_version": 1, "archived": True},
        ),
    ],
)
def test_metadata_and_link_writes_require_session_origin_and_csrf(boundary, method, path, body):
    client, store = boundary
    assert (
        client.request(
            method, path, json=body, headers={"origin": "http://localhost:8000"}
        ).status_code
        == 403
    )
    assert (
        client.request(
            method, path, json=body, headers={**HEADERS, "origin": "https://attacker.example"}
        ).status_code
        == 403
    )
    client.cookies.clear()
    assert client.request(method, path, json=body, headers=HEADERS).status_code == 401
    assert store.events == []


@pytest.mark.parametrize(
    "path",
    [
        "/api/v1/files/configuration",
        PREFIX + "/files",
        PREFIX + f"/files/{FILE}",
        PREFIX + f"/files/{FILE}/content",
        PREFIX + f"/uploads/{UPLOAD}",
        PREFIX + f"/operations/{FILE}/files",
    ],
)
def test_all_private_reads_require_authentication(boundary, path):
    client, store = boundary
    client.cookies.clear()
    assert client.get(path).status_code == 401
    assert store.events == []


@pytest.mark.parametrize(
    "headers,status",
    [
        ({"content-type": "application/pdf"}, 415),
        ({"content-encoding": "gzip"}, 415),
        ({"content-length": "1"}, 422),
        ({"content-length": "invalid"}, 422),
        ({"content-length": str(60 * 1024 * 1024)}, 413),
    ],
)
def test_headers_reject_before_stream(boundary, monkeypatch, headers, status):
    client, store = boundary
    wire(monkeypatch, store)
    response = client.put(
        CONTENT,
        content=PDF,
        headers={**HEADERS, "content-type": "application/octet-stream", **headers},
    )
    assert response.status_code == status
    assert "stage" not in store.events


def test_cross_owner_or_archived_preflight_never_consumes_body(boundary, monkeypatch):
    client, store = boundary

    def forbidden(*args, **kwargs):
        raise LedgerError("not_found", 404, "The requested record was not found.")

    monkeypatch.setattr(DocumentService, "get_upload", forbidden)
    response = client.put(
        CONTENT, content=PDF, headers={**HEADERS, "content-type": "application/octet-stream"}
    )
    assert response.status_code == 404 and store.events == []


def test_expired_session_after_stream_discards_without_commit(boundary, monkeypatch):
    client, store = boundary
    wire(monkeypatch, store)
    calls = 0

    def expire(self, token):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise AuthError("authentication_required", 401)
        return Identity(OWNER, "fictional-owner")

    monkeypatch.setattr(AuthService, "get_session", expire)
    response = client.put(
        CONTENT, content=PDF, headers={**HEADERS, "content-type": "application/octet-stream"}
    )
    assert response.status_code == 401
    assert store.events == ["authorize", "stage", "discard"]


def test_client_disconnect_is_an_interrupted_upload_not_a_server_error(boundary, monkeypatch):
    client, store = boundary
    wire(monkeypatch, store)

    async def disconnect(stream, size):
        raise ClientDisconnect()

    monkeypatch.setattr(store, "stage", disconnect)
    response = client.put(
        CONTENT, content=PDF, headers={**HEADERS, "content-type": "application/octet-stream"}
    )
    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "upload_interrupted"
    assert store.events == ["authorize"]


def test_duplicate_blob_must_exist_and_verify_before_success(boundary, monkeypatch):
    client, store = boundary
    wire(monkeypatch, store, publish=False)

    def corrupt(*args):
        raise FileStoreError("file_integrity_error")

    monkeypatch.setattr(store, "open_blob", corrupt)
    response = client.put(
        CONTENT, content=PDF, headers={**HEADERS, "content-type": "application/octet-stream"}
    )
    assert response.status_code == 503
    assert store.events == ["authorize", "stage", "check", "discard"]


@pytest.mark.parametrize(
    "failure",
    [
        OSError("/private/secret-path"),
        OperationalError("SECRET SQL", {}, Exception("secret")),
        FileStoreError("file_integrity_error"),
    ],
)
def test_storage_errors_are_neutral_and_stage_is_cleaned(boundary, monkeypatch, failure):
    client, store = boundary
    wire(monkeypatch, store)

    def fail(*args):
        raise failure

    monkeypatch.setattr(store, "publish", fail)
    response = client.put(
        CONTENT, content=PDF, headers={**HEADERS, "content-type": "application/octet-stream"}
    )
    assert response.status_code == 503
    assert "secret" not in response.text.lower() and "/private" not in response.text
    assert store.events[-1] == "discard" and "commit" not in store.events


def test_download_uses_verified_handle_and_private_attachment_headers(boundary, monkeypatch):
    client, store = boundary
    file = FileResponse(
        id=FILE,
        ledger_id=LEDGER,
        created_by=OWNER,
        sha256=hashlib.sha256(PDF).hexdigest(),
        byte_size=len(PDF),
        detected_media_type="application/pdf",
        original_filename='虚构"发票.pdf',
        title="Fictional",
        archived=True,
        version=2,
        created_at=NOW,
        updated_at=NOW,
    )
    monkeypatch.setattr(DocumentService, "get_download", lambda *args: (file, "b" * 32))
    response = client.get(PREFIX + f"/files/{FILE}/content")
    assert response.content == PDF and response.status_code == 200
    assert response.headers["content-type"] == "application/pdf"
    assert response.headers["content-disposition"].startswith("attachment;")
    assert "%22" in response.headers["content-disposition"]
    assert response.headers["cache-control"] == "private, no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["content-security-policy"] == "sandbox; default-src 'none'"
    assert store.handle.closed


def test_capabilities_do_not_expose_storage_path(boundary):
    client, _ = boundary
    response = client.get("/api/v1/files/configuration")
    assert response.json() == {
        "max_upload_bytes": 50 * 1024 * 1024,
        "upload_timeout_seconds": 120,
        "supported_media_types": ["application/pdf", "image/jpeg", "image/png", "image/webp"],
    }


def test_openapi_declares_raw_binary_without_multipart(boundary):
    client, _ = boundary
    schema = client.get("/openapi.json").json()
    content = schema["paths"]["/api/v1/ledgers/{ledger_id}/uploads/{upload_id}/content"]["put"][
        "requestBody"
    ]["content"]
    assert content == {
        "application/octet-stream": {"schema": {"type": "string", "format": "binary"}}
    }
