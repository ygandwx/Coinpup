"""Real PostgreSQL metadata transactions with private, durable fixture blobs."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from coinpup_api.config import Settings
from coinpup_api.files.models import FileUpload, OperationFileLink, StoredFile
from coinpup_api.files.router import create_document_router
from coinpup_api.files.schemas import FileUpdate, LinkUpdate, UploadCreate
from coinpup_api.files.service import DocumentService
from coinpup_api.files.storage import FileStore
from coinpup_api.ledger.models import FinancialOperation, Journal, JournalLine
from coinpup_api.ledger.posting import PostingService
from coinpup_api.ledger.posting_schemas import CancellationCreate, OpeningCreate
from coinpup_api.ledger.schemas import AccountCreate, EntityCreate, EntityUpdate
from coinpup_api.ledger.service import LedgerError, LedgerService
from coinpup_api.models import AuthSession
from coinpup_api.security import csrf_token_for, token_digest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

pytestmark = pytest.mark.integration
PDF = b"%PDF-1.7\n% Fictional document service fixture\n%%EOF\n"
OTHER_PDF = PDF.replace(b"Fictional", b"Different")


@pytest.fixture
def setup(structure_database, tmp_path):
    engine, owner = structure_database
    structure = LedgerService(engine)
    entities = [
        structure.create_entity(
            owner,
            EntityCreate(
                kind="personal", name=f"Fictional file ledger {index}", base_asset_id="USD"
            ),
        )
        for index in range(2)
    ]
    account = structure.create_account(
        owner,
        entities[0].ledger.id,
        AccountCreate(name="Fictional bank", kind="bank", asset_ids=["USD"]),
    )
    posting = PostingService(engine)
    operation = posting.post_opening(
        owner,
        entities[0].ledger.id,
        OpeningCreate(
            account_id=account.id, asset_id="USD", amount="100", transaction_date="2026-01-01"
        ),
        "fictional-document-opening",
    )
    return {
        "engine": engine,
        "owner": owner,
        "ledger": entities[0].ledger.id,
        "other": entities[1].ledger.id,
        "entity": entities[0],
        "structure": structure,
        "posting": posting,
        "operation": operation,
        "service": DocumentService(engine, 1024),
        "store": FileStore(tmp_path / "private", 1024, 5),
    }


def reserve(s, ledger=None, content=PDF, operation=None, name="fictional.pdf", identifier=None):
    payload = UploadCreate(
        id=identifier or uuid4(),
        original_filename=name,
        declared_size=len(content),
        operation_id=operation,
    )
    return payload, s["service"].reserve_upload(s["owner"], ledger or s["ledger"], payload)


def stage(s, content=PDF):
    async def chunks():
        for index in range(0, len(content), 7):
            yield content[index : index + 7]

    return asyncio.run(s["store"].stage(chunks(), len(content)))


def complete(s, upload, content=PDF, ledger=None):
    ledger = ledger or s["ledger"]
    staged = stage(s, content)
    try:
        if s["service"].needs_publish(s["owner"], ledger, upload.id, staged):
            key = s["store"].publish(staged)
        else:
            with s["store"].open_blob(
                *s["service"].get_existing_blob(s["owner"], ledger, upload.id, staged)
            ):
                pass
            key = None
        return s["service"].finalize_upload(s["owner"], ledger, upload.id, staged, key)
    finally:
        s["store"].discard(staged)


def counts(s):
    with Session(s["engine"]) as session:
        return tuple(
            session.scalar(select(func.count()).select_from(table))
            for table in (
                FileUpload,
                StoredFile,
                OperationFileLink,
                FinancialOperation,
                Journal,
                JournalLine,
            )
        )


def assert_error(code, operation):
    with pytest.raises(LedgerError) as error:
        operation()
    assert error.value.code == code


def test_reservation_is_pending_retryable_and_manifest_frozen(setup):
    s = setup
    payload, upload = reserve(s)
    assert upload.state == "pending" and upload.response is None
    assert counts(s)[:3] == (1, 0, 0)
    assert s["service"].reserve_upload(s["owner"], s["ledger"], payload) == upload
    assert_error(
        "upload_manifest_conflict",
        lambda: s["service"].reserve_upload(
            s["owner"], s["ledger"], payload.model_copy(update={"original_filename": "changed.pdf"})
        ),
    )
    assert_error(
        "file_too_large",
        lambda: s["service"].reserve_upload(
            s["owner"],
            s["ledger"],
            UploadCreate(id=uuid4(), original_filename="large.pdf", declared_size=1025),
        ),
    )
    assert_error("not_found", lambda: s["service"].get_file(s["owner"], s["ledger"], upload.id))


def test_content_receipt_and_operation_link_commit_together_without_financial_change(setup):
    s = setup
    original_counts = counts(s)[3:]
    _, upload = reserve(s, operation=s["operation"].id, name="虚构发票.pdf")
    result = complete(s, upload)
    assert result.upload_id == upload.id and not result.duplicate and result.link_id is not None
    stored = s["service"].get_upload(s["owner"], s["ledger"], upload.id)
    assert stored.state == "ready" and stored.response == result and stored.completed_at is not None
    metadata, key = s["service"].get_download(s["owner"], s["ledger"], result.file_id)
    with s["store"].open_blob(key, metadata.sha256, metadata.byte_size) as handle:
        assert handle.read() == PDF
    assert metadata.original_filename == "虚构发票.pdf" and "blob_key" not in metadata.model_dump()
    assert counts(s) == (1, 1, 1, *original_counts)


def test_dedup_is_ledger_local_and_preserves_first_content_metadata(setup):
    s = setup
    _, first = reserve(s, name="first.pdf")
    original = complete(s, first)
    _, second = reserve(s, name="second.pdf")
    duplicate = complete(s, second)
    assert duplicate.duplicate and duplicate.file_id == original.file_id
    assert (
        s["service"].get_file(s["owner"], s["ledger"], original.file_id).original_filename
        == "first.pdf"
    )
    _, other = reserve(s, ledger=s["other"])
    separate = complete(s, other, ledger=s["other"])
    assert not separate.duplicate and separate.file_id != original.file_id
    assert counts(s)[:3] == (3, 2, 0)
    assert len(list((s["store"].root / "blobs").iterdir())) == 2


def test_ready_replay_survives_metadata_link_and_entity_archive_but_checks_bytes(setup):
    s = setup
    payload, upload = reserve(s, operation=s["operation"].id)
    result = complete(s, upload)
    s["service"].update_link(
        s["owner"],
        s["ledger"],
        s["operation"].id,
        result.file_id,
        LinkUpdate(expected_version=1, archived=True),
    )
    s["service"].update_file(
        s["owner"],
        s["ledger"],
        result.file_id,
        FileUpdate(expected_version=1, title="Archived fictional evidence", archived=True),
    )
    s["structure"].update_entity(
        s["owner"], s["entity"].id, EntityUpdate(expected_version=1, archived=True)
    )
    before = counts(s)
    assert complete(s, upload) == result
    assert s["service"].reserve_upload(s["owner"], s["ledger"], payload).response == result
    assert_error("upload_content_conflict", lambda: complete(s, upload, content=OTHER_PDF))
    assert counts(s) == before
    assert s["service"].get_file(s["owner"], s["ledger"], result.file_id).version == 2
    assert_error("entity_archived", lambda: reserve(s))


def test_pending_cannot_complete_after_entity_archive(setup):
    s = setup
    _, upload = reserve(s)
    staged = stage(s)
    key = s["store"].publish(staged)
    s["structure"].update_entity(
        s["owner"], s["entity"].id, EntityUpdate(expected_version=1, archived=True)
    )
    assert_error(
        "entity_archived",
        lambda: s["service"].get_upload(s["owner"], s["ledger"], upload.id, for_upload=True),
    )
    assert_error(
        "entity_archived",
        lambda: s["service"].finalize_upload(s["owner"], s["ledger"], upload.id, staged, key),
    )
    assert counts(s)[:3] == (1, 0, 0)
    assert s["service"].get_upload(s["owner"], s["ledger"], upload.id).state == "pending"


def test_new_links_allow_cancelled_operation_and_archive_without_erasing_evidence(setup):
    s = setup
    state = s["posting"].cancel_operation(
        s["owner"],
        s["ledger"],
        s["operation"].id,
        CancellationCreate(expected_version=1, reason="Fictional cancellation"),
        "fixture-cancel",
    )
    _, upload = reserve(s)
    result = complete(s, upload)
    link = s["service"].link_file(s["owner"], s["ledger"], state.id, result.file_id)
    assert s["service"].link_file(s["owner"], s["ledger"], state.id, result.file_id) == link
    archived = s["service"].update_link(
        s["owner"],
        s["ledger"],
        state.id,
        result.file_id,
        LinkUpdate(expected_version=1, archived=True),
    )
    assert archived.version == 2
    assert s["service"].link_file(s["owner"], s["ledger"], state.id, result.file_id) == archived
    assert s["service"].list_operation_files(s["owner"], s["ledger"], state.id) == [archived]
    assert (
        s["service"].list_operation_files(s["owner"], s["ledger"], state.id, include_archived=False)
        == []
    )
    assert s["posting"].get_operation(s["owner"], s["ledger"], state.id) == state
    assert_error(
        "version_conflict",
        lambda: s["service"].update_link(
            s["owner"],
            s["ledger"],
            state.id,
            result.file_id,
            LinkUpdate(expected_version=1, archived=False),
        ),
    )


def test_cross_ledger_and_owner_references_are_generic_not_found(setup):
    s = setup
    _, upload = reserve(s)
    result = complete(s, upload)
    outsider = uuid4()  # The current product permits only one initialized administrator.
    operations = [
        lambda: s["service"].get_upload(s["owner"], s["other"], upload.id),
        lambda: s["service"].get_download(s["owner"], s["other"], result.file_id),
        lambda: s["service"].get_file(outsider, s["ledger"], result.file_id),
        lambda: reserve(s, ledger=s["other"], operation=s["operation"].id),
        lambda: s["service"].link_file(s["owner"], s["other"], s["operation"].id, result.file_id),
        lambda: reserve(s, ledger=s["other"], identifier=upload.id),
    ]
    for operation in operations:
        assert_error("not_found", operation)
    assert counts(s)[:3] == (1, 1, 0)


def test_archive_and_metadata_optimistic_version_never_mutate_content(setup):
    s = setup
    _, upload = reserve(s)
    result = complete(s, upload)
    changed = s["service"].update_file(
        s["owner"],
        s["ledger"],
        result.file_id,
        FileUpdate(expected_version=1, title="New label", archived=True),
    )
    assert changed.version == 2 and changed.sha256 == result.sha256
    assert s["service"].list_files(s["owner"], s["ledger"]) == []
    assert s["service"].list_files(s["owner"], s["ledger"], include_archived=True) == [changed]
    assert_error(
        "file_archived",
        lambda: s["service"].link_file(s["owner"], s["ledger"], s["operation"].id, result.file_id),
    )
    assert_error(
        "version_conflict",
        lambda: s["service"].update_file(
            s["owner"], s["ledger"], result.file_id, FileUpdate(expected_version=1, title="Stale")
        ),
    )
    restored = s["service"].update_file(
        s["owner"], s["ledger"], result.file_id, FileUpdate(expected_version=2, archived=False)
    )
    assert restored.version == 3 and restored.original_filename == upload.original_filename


def test_failure_after_blob_publish_rolls_back_file_link_and_ready_receipt(setup, monkeypatch):
    s = setup
    _, upload = reserve(s, operation=s["operation"].id)
    staged = stage(s)
    key = s["store"].publish(staged)

    def fail(*args):
        raise LedgerError("fixture_failure", 503, "Fictional failure")

    with monkeypatch.context() as context:
        context.setattr(s["service"], "_link", fail)
        assert_error(
            "fixture_failure",
            lambda: s["service"].finalize_upload(s["owner"], s["ledger"], upload.id, staged, key),
        )
    assert counts(s)[:3] == (1, 0, 0)
    assert s["service"].get_upload(s["owner"], s["ledger"], upload.id).state == "pending"
    assert (s["store"].root / "blobs" / key).exists()  # Private orphan, not a visible broken file.
    recovered = s["service"].finalize_upload(s["owner"], s["ledger"], upload.id, staged, key)
    assert recovered.link_id is not None and counts(s)[:3] == (1, 1, 1)


@pytest.mark.parametrize("same_reservation", [True, False])
def test_concurrent_completion_returns_one_canonical_file_and_stable_receipts(
    setup, same_reservation
):
    s = setup
    _, first = reserve(s, operation=s["operation"].id)
    second = first if same_reservation else reserve(s, operation=s["operation"].id)[1]
    staged = [stage(s), stage(s)]
    keys = [s["store"].publish(item) for item in staged]

    def finish(index):
        return s["service"].finalize_upload(
            s["owner"], s["ledger"], (first, second)[index].id, staged[index], keys[index]
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(finish, range(2)))
    assert results[0].file_id == results[1].file_id and results[0].link_id == results[1].link_id
    if same_reservation:
        assert results[0] == results[1]
    else:
        assert sorted(item.duplicate for item in results) == [False, True]
    assert counts(s)[:3] == (1 if same_reservation else 2, 1, 1)


def test_competing_content_for_same_reservation_only_one_can_commit(setup):
    s = setup
    _, upload = reserve(s)
    staged = [stage(s), stage(s, OTHER_PDF)]
    keys = [s["store"].publish(item) for item in staged]

    def finish(index):
        try:
            return s["service"].finalize_upload(
                s["owner"], s["ledger"], upload.id, staged[index], keys[index]
            )
        except LedgerError as error:
            return error.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(finish, range(2)))
    assert sum(item == "upload_content_conflict" for item in results) == 1
    assert counts(s)[:3] == (1, 1, 0)


@pytest.fixture
def http(setup):
    s = setup
    token = "fictional-document-integration-session"
    with Session(s["engine"]) as session, session.begin():
        session.add(
            AuthSession(
                token_hash=token_digest(token),
                administrator_id=s["owner"],
                expires_at=datetime.now(UTC) + timedelta(hours=1),
            )
        )
    app = FastAPI()
    app.include_router(
        create_document_router(
            Settings(
                _env_file=None,
                environment="test",
                files_directory=s["store"].root,
                max_upload_bytes=1024,
            ),
            s["engine"],
            s["store"],
        )
    )
    with TestClient(app) as client:
        client.cookies.set("coinpup_session", token)
        client.headers.update(
            {"origin": "http://localhost:8000", "x-csrf-token": csrf_token_for(token)}
        )
        yield client, s


def test_real_http_reservation_stream_download_and_identical_retry(http):
    client, s = http
    prefix = f"/api/v1/ledgers/{s['ledger']}"
    identifier = uuid4()
    body = {
        "id": str(identifier),
        "original_filename": "fictional-photo.jpg",
        "declared_size": len(PDF),
        "operation_id": str(s["operation"].id),
    }
    reservation = client.post(prefix + "/uploads", json=body)
    assert reservation.status_code == 201 and reservation.json()["state"] == "pending"
    path = prefix + f"/uploads/{identifier}/content"
    result = client.put(path, content=PDF, headers={"content-type": "application/octet-stream"})
    assert result.status_code == 200 and result.json()["media_type"] == "application/pdf"
    file_id = result.json()["file_id"]
    assert client.get(prefix + f"/files/{file_id}/content").content == PDF
    assert (
        client.put(path, content=PDF, headers={"content-type": "application/octet-stream"}).json()
        == result.json()
    )
    assert len(list((s["store"].root / "blobs").iterdir())) == 1
    assert counts(s)[:3] == (1, 1, 1)


def test_corrupt_canonical_blob_blocks_ready_replay_and_duplicate_completion(http):
    client, s = http
    _, first = reserve(s)
    result = complete(s, first)
    _, duplicate = reserve(s)
    metadata, key = s["service"].get_download(s["owner"], s["ledger"], result.file_id)
    (s["store"].root / "blobs" / key).write_bytes(OTHER_PDF)
    for upload in (first, duplicate):
        response = client.put(
            f"/api/v1/ledgers/{s['ledger']}/uploads/{upload.id}/content",
            content=PDF,
            headers={"content-type": "application/octet-stream"},
        )
        assert (
            response.status_code == 503
            and response.json()["detail"]["code"] == "file_integrity_error"
        )
    assert s["service"].get_upload(s["owner"], s["ledger"], duplicate.id).state == "pending"
    assert counts(s)[:3] == (2, 1, 0)
    assert metadata.byte_size == len(OTHER_PDF)


def test_invalid_stream_cannot_create_metadata_or_complete_reservation(http):
    client, s = http
    invalid = b"<html>fictional unsupported content</html>"
    _, upload = reserve(s, content=invalid)
    path = f"/api/v1/ledgers/{s['ledger']}/uploads/{upload.id}/content"
    response = client.put(
        path, content=invalid, headers={"content-type": "application/octet-stream"}
    )
    assert response.status_code == 415
    assert counts(s)[:3] == (1, 0, 0)
    assert s["service"].get_upload(s["owner"], s["ledger"], upload.id).state == "pending"
    assert not list((s["store"].root / "staging").iterdir())


def test_chunked_upload_enforces_actual_limit_and_same_reservation_can_retry(http):
    client, s = http
    _, upload = reserve(s)
    path = f"/api/v1/ledgers/{s['ledger']}/uploads/{upload.id}/content"
    response = client.put(
        path,
        content=iter([PDF, b"extra"]),
        headers={"content-type": "application/octet-stream"},
    )
    assert response.status_code == 413
    assert s["service"].get_upload(s["owner"], s["ledger"], upload.id).state == "pending"
    assert counts(s)[:3] == (1, 0, 0)
    retry = client.put(
        path,
        content=iter([PDF[:10], PDF[10:]]),
        headers={"content-type": "application/octet-stream"},
    )
    assert retry.status_code == 200 and counts(s)[:3] == (1, 1, 0)
