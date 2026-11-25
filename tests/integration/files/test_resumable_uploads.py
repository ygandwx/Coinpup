"""Resumable upload authorization, actual interruption, durable offset and original receipt."""

import hashlib
from uuid import UUID, uuid4

import anyio
import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from coinpup_api.config import Settings
from coinpup_api.files.resume_models import CHUNK_BYTES, UploadChunk, UploadResume
from coinpup_api.files.resume_service import ResumableUploads
from coinpup_api.files.router import create_document_router
from coinpup_api.files.schemas import UploadCreate
from coinpup_api.files.service import DocumentService
from coinpup_api.files.storage import FileStore
from coinpup_api.ledger.schemas import EntityUpdate
from coinpup_api.ledger.service import LedgerError
from coinpup_api.sync.devices import DeviceRegistry
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, insert, select, update
from sqlalchemy.exc import IntegrityError

from tests.integration.conftest import resumable_upload_migration
from tests.integration.files.test_files_service import setup as setup

pytestmark = pytest.mark.integration
PDF = b"%PDF-1.7\n" + b"% Synthetic part\n" * 65536 + b"\n%%EOF\n"
assert len(PDF) > CHUNK_BYTES


def digest(data):
    return hashlib.sha256(data).hexdigest()


@pytest.fixture
def resume_http(setup, authenticated_client, tmp_path):
    authenticated, _, _ = authenticated_client
    device, peer = uuid4(), uuid4()
    service = DeviceRegistry(setup["engine"])
    for identifier in (device, peer):
        service.register(setup["owner"], identifier)
    settings = Settings(_env_file=None, environment="test", max_upload_bytes=4 * CHUNK_BYTES)
    store = FileStore(tmp_path / "resumable", settings.max_upload_bytes, 10)
    app = FastAPI()
    app.include_router(create_document_router(settings, setup["engine"], store))
    with TestClient(app) as client:
        client.cookies.update(authenticated.cookies)
        client.headers.update(
            {
                "origin": "http://localhost:8000",
                "x-csrf-token": authenticated.headers["x-csrf-token"],
            }
        )
        yield client, setup, store, device, peer


def headers(device, *, binary=False):
    result = {"X-Coinpup-Device-Id": str(device)}
    if binary:
        result["content-type"] = "application/octet-stream"
    return result


def reserve(client, s, data):
    root = f"/api/v1/ledgers/{s['ledger']}/uploads"
    upload = uuid4()
    response = client.post(
        root,
        json={
            "id": str(upload),
            "original_filename": "Fictional.pdf",
            "declared_size": len(data),
            "operation_id": str(s["operation"].id),
        },
    )
    assert response.status_code == 201, response.json()
    return upload, f"{root}/{upload}"


def create(client, s, device, *, data=PDF, upload_hash=None):
    upload, url = reserve(client, s, data)
    initialized = client.put(
        url + "/resumable",
        json={"sha256": upload_hash or digest(data)},
        headers=headers(device),
    )
    assert initialized.status_code == 200, initialized.json()
    assert initialized.json()["next_offset"] == 0
    return upload, url


def interrupt(client, path, device, partial):
    async def request():
        messages = iter(
            (
                {"type": "http.request", "body": partial, "more_body": True},
                {"type": "http.disconnect"},
            )
        )
        response = []

        async def receive():
            return next(messages)

        async def send(message):
            response.append(message)

        request_headers = headers(device, binary=True) | {
            "origin": "http://localhost:8000",
            "x-csrf-token": client.headers["x-csrf-token"],
            "cookie": f"coinpup_session={client.cookies.get('coinpup_session')}",
        }
        await client.app(
            {
                "type": "http",
                "method": "PUT",
                "path": path,
                "scheme": "http",
                "root_path": "",
                "query_string": b"",
                "http_version": "1.1",
                "headers": [
                    (key.lower().encode(), value.encode()) for key, value in request_headers.items()
                ],
                "server": ("localhost", 8000),
                "client": ("127.0.0.1", 10000),
            },
            receive,
            send,
        )
        return response

    return anyio.run(request)


def test_committed_parts_resume_after_real_disconnect_and_complete_once(resume_http):
    client, s, store, device, _ = resume_http
    upload, url = create(client, s, device)
    endpoint = url + "/resumable"
    first, last = PDF[:CHUNK_BYTES], PDF[CHUNK_BYTES:]
    part = endpoint + "/chunks/"
    raw = headers(device, binary=True)
    assert client.put(part + str(CHUNK_BYTES), content=last, headers=raw).status_code == 409
    original = client.put(part + "0", content=first, headers=raw)
    assert original.status_code == 200 and original.json()["next_offset"] == CHUNK_BYTES
    assert client.put(part + "0", content=first, headers=raw).content == original.content
    assert client.put(part + "0", content=b"z" + first[1:], headers=raw).status_code == 409

    interrupted = interrupt(client, part + str(CHUNK_BYTES), device, last[:100])
    assert interrupted[0]["status"] == 400
    assert client.get(endpoint, headers=headers(device)).json()["next_offset"] == CHUNK_BYTES
    assert client.post(endpoint + "/complete", headers=headers(device)).status_code == 409
    complete_parts = client.put(part + str(CHUNK_BYTES), content=last, headers=raw)
    assert complete_parts.json()["next_offset"] == len(PDF)
    result = client.post(endpoint + "/complete", headers=headers(device))
    assert result.status_code == 200, result.json()
    saved = result.json()
    assert saved["sha256"] == digest(PDF) and saved["byte_size"] == len(PDF) and saved["link_id"]
    abandon = client.post(endpoint + "/abandon")
    assert abandon.status_code == 409 and abandon.json()["detail"]["code"] == "upload_complete"
    assert client.post(endpoint + "/complete", headers=headers(device)).content == result.content
    assert client.put(part + "0", content=first, headers=raw).content == original.content
    assert client.get(endpoint, headers=headers(device)).json()["state"] == "ready"

    with s["engine"].connect() as connection:
        stored = connection.execute(
            select(UploadChunk.byte_offset, UploadChunk.payload)
            .where(UploadChunk.upload_id == upload)
            .order_by(UploadChunk.byte_offset)
        ).all()
    assert stored == [(0, None), (CHUNK_BYTES, None)]
    file = DocumentService(s["engine"]).get_download(
        s["owner"], s["ledger"], UUID(saved["file_id"])
    )
    with store.open_blob(file[1], saved["sha256"], saved["byte_size"]) as stream:
        assert stream.read() == PDF


def test_original_reservation_isolates_ledger_and_device_including_legacy_fallback(resume_http):
    client, s, _, device, peer = resume_http
    _, url = create(client, s, device)
    endpoint = url + "/resumable"
    other = endpoint.replace(str(s["ledger"]), str(s["other"]))
    assert client.get(other, headers=headers(device)).status_code == 404
    assert client.get(endpoint, headers=headers(peer)).status_code == 404
    assert client.get(endpoint).status_code == 422
    assert (
        client.put(endpoint, json={"sha256": "0" * 64}, headers=headers(device)).status_code == 409
    )
    assert (
        client.put(
            url + "/content", content=PDF, headers={"content-type": "application/octet-stream"}
        ).status_code
        == 409
    )
    different = PDF.replace(b"Synthetic", b"Different", 1)
    assert (
        client.put(
            url + "/content", content=different, headers=headers(device, binary=True)
        ).status_code
        == 409
    )
    assert client.get(endpoint, headers=headers(device)).json()["next_offset"] == 0

    fallback_upload, fallback = create(client, s, device)
    assert (
        client.put(
            fallback + "/resumable/chunks/0",
            content=PDF[:CHUNK_BYTES],
            headers=headers(device, binary=True),
        ).status_code
        == 200
    )
    prior = client.put(fallback + "/content", content=PDF, headers=headers(device, binary=True))
    assert prior.status_code == 200, prior.json()
    assert str(fallback_upload) == prior.json()["upload_id"]
    assert (
        client.post(fallback + "/resumable/complete", headers=headers(device)).content
        == prior.content
    )
    with s["engine"].connect() as connection:
        assert connection.execute(
            select(UploadChunk.payload).where(UploadChunk.upload_id == fallback_upload)
        ).one() == (None,)

    DeviceRegistry(s["engine"]).retire(s["owner"], device)
    response = client.put(
        endpoint + "/chunks/0", content=PDF[:CHUNK_BYTES], headers=headers(device, binary=True)
    )
    assert (
        response.status_code == 409 and response.json()["detail"]["code"] == "sync_device_retired"
    )
    assert client.get(endpoint, headers=headers(device)).json()["next_offset"] == 0


def test_ready_legacy_upload_refuses_retroactive_binding_and_keeps_original_replay(resume_http):
    client, s, _, device, _ = resume_http
    upload, url = reserve(client, s, PDF)
    original = client.put(
        url + "/content", content=PDF, headers={"content-type": "application/octet-stream"}
    )
    assert original.status_code == 200, original.json()
    bind = client.put(url + "/resumable", json={"sha256": digest(PDF)}, headers=headers(device))
    assert bind.status_code == 409 and bind.json()["detail"]["code"] == "upload_complete"
    abandon = client.post(url + "/resumable/abandon")
    assert abandon.status_code == 409 and abandon.json()["detail"]["code"] == "upload_complete"
    assert client.get(url + "/resumable", headers=headers(device)).status_code == 404
    with pytest.raises(IntegrityError) as rejected, s["engine"].begin() as connection:
        connection.execute(
            insert(UploadResume).values(
                upload_id=upload,
                ledger_id=s["ledger"],
                owner_id=s["owner"],
                device_id=device,
                sha256=digest(PDF),
            )
        )
    assert rejected.value.orig.diag.constraint_name == "ck_upload_resume_scope"
    replay = client.put(
        url + "/content", content=PDF, headers={"content-type": "application/octet-stream"}
    )
    assert replay.status_code == 200 and replay.content == original.content
    file = client.get(f"/api/v1/ledgers/{s['ledger']}/files/{original.json()['file_id']}/content")
    assert file.status_code == 200 and file.content == PDF


def test_corrupt_upload_can_be_abandoned_after_retirement_with_immutable_receipts(resume_http):
    client, s, _, device, _ = resume_http
    manifest_hash = digest(PDF + b"different")
    upload, url = create(client, s, device, upload_hash=manifest_hash)
    endpoint = url + "/resumable"
    part = endpoint + "/chunks/"
    raw = headers(device, binary=True)
    original = client.put(part + "0", content=PDF[:CHUNK_BYTES], headers=raw)
    assert original.status_code == 200
    last = client.put(part + str(CHUNK_BYTES), content=PDF[CHUNK_BYTES:], headers=raw)
    assert last.status_code == 200
    failed = client.post(endpoint + "/complete", headers=headers(device))
    assert (
        failed.status_code == 409 and failed.json()["detail"]["code"] == "upload_content_conflict"
    )

    abandon_url = endpoint + "/abandon"
    other_ledger = abandon_url.replace(str(s["ledger"]), str(s["other"]))
    assert client.post(other_ledger).status_code == 404
    assert (
        client.post(abandon_url, headers={"x-csrf-token": "fictional-invalid"}).status_code == 403
    )
    upload_service = ResumableUploads(s["engine"])
    with pytest.raises(LedgerError) as other_owner:
        upload_service.abandon(uuid4(), s["ledger"], upload)
    assert other_owner.value.status == 404

    DeviceRegistry(s["engine"]).retire(s["owner"], device)
    s["structure"].update_entity(
        s["owner"], s["entity"].id, EntityUpdate(expected_version=1, archived=True)
    )
    accepted = client.post(abandon_url)
    assert accepted.status_code == 200, accepted.json()
    assert accepted.json() == last.json() | {"state": "abandoned"}
    assert client.post(abandon_url).content == accepted.content
    assert client.get(endpoint, headers=headers(device)).content == accepted.content
    assert (
        client.put(endpoint, json={"sha256": manifest_hash}, headers=headers(device)).content
        == accepted.content
    )
    assert (
        client.put(part + "0", content=PDF[:CHUNK_BYTES], headers=raw).content == original.content
    )
    assert (
        client.put(part + str(CHUNK_BYTES), content=PDF[CHUNK_BYTES:], headers=raw).content
        == last.content
    )
    assert client.put(part + "0", content=b"x" + PDF[1:CHUNK_BYTES], headers=raw).status_code == 409
    for rejected in (
        client.post(endpoint + "/complete", headers=headers(device)),
        client.put(url + "/content", content=PDF, headers=raw),
        client.put(part + str(2 * CHUNK_BYTES), content=b"x", headers=raw),
    ):
        assert (
            rejected.status_code == 409 and rejected.json()["detail"]["code"] == "upload_abandoned"
        )

    with s["engine"].connect() as connection:
        stored = connection.execute(
            select(
                UploadChunk.byte_offset,
                UploadChunk.byte_size,
                UploadChunk.sha256,
                UploadChunk.payload,
            )
            .where(UploadChunk.upload_id == upload)
            .order_by(UploadChunk.byte_offset)
        ).all()
        marker = connection.scalar(
            select(UploadResume.abandoned_at).where(UploadResume.upload_id == upload)
        )
    assert stored == [
        (0, CHUNK_BYTES, digest(PDF[:CHUNK_BYTES]), None),
        (CHUNK_BYTES, len(PDF) - CHUNK_BYTES, digest(PDF[CHUNK_BYTES:]), None),
    ]
    assert marker is not None
    with pytest.raises(IntegrityError) as immutable, s["engine"].begin() as connection:
        connection.execute(
            update(UploadResume).where(UploadResume.upload_id == upload).values(abandoned_at=None)
        )
    assert immutable.value.orig.diag.constraint_name == "ck_upload_resume_immutable"


def test_abandonment_wins_after_blob_publish_and_before_completion_commit(resume_http, monkeypatch):
    client, s, store, device, _ = resume_http
    upload, url = create(client, s, device)
    endpoint = url + "/resumable"
    for offset in (0, CHUNK_BYTES):
        chunk = PDF[offset : offset + CHUNK_BYTES]
        assert (
            client.put(
                f"{endpoint}/chunks/{offset}", content=chunk, headers=headers(device, binary=True)
            ).status_code
            == 200
        )
    publish = store.publish
    service = ResumableUploads(s["engine"])

    def publish_then_abandon(staged):
        key = publish(staged)
        assert service.abandon(s["owner"], s["ledger"], upload).state == "abandoned"
        return key

    monkeypatch.setattr(store, "publish", publish_then_abandon)
    complete = client.post(endpoint + "/complete", headers=headers(device))
    assert complete.status_code == 409 and complete.json()["detail"]["code"] == "upload_abandoned"
    assert client.get(url).json()["response"] is None
    assert client.get(f"/api/v1/ledgers/{s['ledger']}/files").json() == []


def test_owner_pending_byte_quota_and_guarded_downgrade(setup):
    engine, owner, ledger = (setup[name] for name in ("engine", "owner", "ledger"))
    device, peer = uuid4(), uuid4()
    registry = DeviceRegistry(engine)
    for identifier in (device, peer):
        registry.register(owner, identifier)
    service = ResumableUploads(engine, CHUNK_BYTES)
    data = b"x" * CHUNK_BYTES
    queued = []
    for i in range(5):
        upload = uuid4()
        service.reserve_upload(
            owner,
            ledger,
            UploadCreate(id=upload, original_filename="Fictional.pdf", declared_size=len(data)),
        )
        service.initialize(owner, ledger, upload, device if i < 4 else peer, digest(data))
        queued.append(upload)
    for upload in queued[:4]:
        assert service.append(owner, ledger, upload, device, 0, data).next_offset == CHUNK_BYTES
    with pytest.raises(LedgerError) as quota:
        service.append(owner, ledger, queued[-1], peer, 0, data)
    assert quota.value.code == "upload_staging_limit"
    assert service.append(owner, ledger, queued[0], device, 0, data).next_offset == CHUNK_BYTES
    with pytest.raises(IntegrityError) as mismatch, engine.begin() as connection:
        connection.execute(
            insert(UploadChunk).values(
                upload_id=queued[-1],
                ledger_id=ledger,
                byte_offset=0,
                byte_size=len(data),
                sha256="0" * 64,
                payload=data,
            )
        )
    assert mismatch.value.orig.diag.constraint_name == "ck_file_upload_chunk_content"
    registry.retire(owner, device)
    assert service.abandon(owner, ledger, queued[0]).state == "abandoned"
    assert service.append(owner, ledger, queued[-1], peer, 0, data).next_offset == CHUNK_BYTES
    with engine.connect() as connection:
        stored = connection.execute(select(func.count(), func.count(UploadChunk.payload))).one()
    assert stored == (5, 4)
    with pytest.raises(IntegrityError) as downgrade:
        with (
            engine.begin() as connection,
            Operations.context(MigrationContext.configure(connection)),
        ):
            resumable_upload_migration()["downgrade"]()
    assert downgrade.value.orig.diag.constraint_name == "ck_upload_resume_downgrade"
