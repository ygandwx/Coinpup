"""Synthetic real-service exercise, called only by the guarded disposable Compose checker."""

import asyncio
import base64
import json
import os
import tempfile
from dataclasses import replace
from datetime import date
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from _bundle_archive import verified_bundle
from _database_archive import ArchiveError, read_target, require_posix
from backup_bundle import backup_bundle
from check_backup_restore import change_state, snapshot, verify_change_continuation
from check_control_restore import seed_controls, verify_controls
from check_ocr_confirmation_restore import seed_confirmations, verify_confirmations
from check_period_restore import seed_periods, verify_periods
from check_reminder_restore import seed_reminders, verify_reminders
from restore_bundle import restore_bundle


def fictional_pdf(label="Fictional Coinpup bundle fixture", *, pages=1, identity=""):
    if pages not in (1, 2):
        raise ValueError("Fictional fixture supports one or two pages")
    content = f"BT /F1 12 Tf 50 100 Td ({label}) Tj ET".encode("ascii")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 200] "
        b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        f"<< /Length {len(content)} >>\nstream\n".encode() + content + b"\nendstream",
    ]
    if pages == 2:
        objects[1] = b"<< /Type /Pages /Kids [3 0 R 6 0 R] /Count 2 >>"
        objects.append(objects[2])
    if identity:
        if not identity.isascii() or any(char in identity for char in "()\\\r\n"):
            raise ValueError("Fictional identity must be a plain ASCII label")
        objects.append(f"<< /Subject ({identity}) >>".encode("ascii"))
    info = f" /Info {len(objects)} 0 R" if identity else ""
    count = len(objects) + 1
    output, offsets = b"%PDF-1.4\n", [0]
    for index, item in enumerate(objects, 1):
        offsets.append(len(output))
        output += f"{index} 0 obj\n".encode() + item + b"\nendobj\n"
    start = len(output)
    output += f"xref\n0 {count}\n0000000000 65535 f \n".encode()
    output += b"".join(f"{offset:010d} 00000 n \n".encode() for offset in offsets[1:])
    return (
        output
        + f"trailer\n<< /Size {count} /Root 1 0 R{info} >>\nstartxref\n{start}\n%%EOF\n".encode()
    )


PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aK1kAAAAASUVORK5CYII="
)


async def stage_bytes(store, content):
    async def chunks():
        for offset in range(0, len(content), 17):
            yield content[offset : offset + 17]

    return await store.stage(chunks(), len(content))


def upload_fixture(service, store, owner, ledger, content, filename, operation=None, request=None):
    from coinpup_api.files.schemas import UploadCreate

    request = request or UploadCreate(
        id=uuid4(), original_filename=filename, declared_size=len(content), operation_id=operation
    )
    service.reserve_upload(owner, ledger, request)
    staged = asyncio.run(stage_bytes(store, content))
    try:
        key = None
        if service.needs_publish(owner, ledger, request.id, staged):
            key = store.publish(staged)
        else:
            existing = service.get_existing_blob(owner, ledger, request.id, staged)
            with store.open_blob(*existing):
                pass
        receipt = service.finalize_upload(owner, ledger, request.id, staged, key)
    finally:
        store.discard(staged)
    return {"ledger": ledger, "request": request, "receipt": receipt, "content": content}


def fictional_ocr_metadata(engine, owner, ledger_id, file_id):
    """Exercise real queue services with explicit fictional candidates, not recognition."""
    from coinpup_api.ledger.service import LedgerService, _touch
    from coinpup_api.ocr.contracts import Candidate, Completion, JobCreate
    from coinpup_api.ocr.models import OcrDraft, OcrJob
    from coinpup_api.ocr.queue import OcrQueueService
    from sqlalchemy import func, select, text

    configuration = {
        "lease_seconds": 300,
        "retry_seconds": 0,
        "processing": {"profile": "fictional-bundle-service-v1"},
    }
    request = JobCreate(intent_id=uuid4(), file_id=file_id)
    completion = Completion(
        summary={"pages": 1, "manual_pages": 1},
        candidates=(
            Candidate(
                source_key="fixture:1",
                recognized={"amount": "100.00", "note": "Fictional candidate; no OCR ran"},
                evidence={"page": 1, "route": "manual_fixture"},
                fields={"amount": "100.00"},
            ),
        ),
    )
    queue = OcrQueueService(engine)
    created = queue.create_job(owner, ledger_id, request, configuration=configuration)
    lease = queue.claim(owner)
    if lease is None or lease.job_id != created.id:
        raise ArchiveError("Fictional queue fixture did not claim its expected task.")
    completed = queue.finish(lease, completion)
    draft_id = completed.result.draft_ids[0]
    scope = LedgerService(engine)
    with scope._transaction(owner, write=True) as session:
        scope._ledger(session, owner, ledger_id, write=True)
        draft = session.get(OcrDraft, draft_id)
        draft.fields = {"amount": "99.00"}
        _touch(draft)
        session.flush()
    interrupted = queue.create_job(
        owner,
        ledger_id,
        JobCreate(intent_id=uuid4(), file_id=file_id),
        configuration=configuration,
    )
    old_lease = queue.claim(owner)
    if old_lease is None or old_lease.job_id != interrupted.id:
        raise ArchiveError("Fictional interruption fixture did not claim its expected task.")
    with scope._transaction(owner, write=True) as session:
        scope._ledger(session, owner, ledger_id, write=True)
        job = session.scalar(select(OcrJob).where(OcrJob.id == interrupted.id).with_for_update())
        job.lease_until = func.clock_timestamp() - text("INTERVAL '1 second'")
        _touch(job)
    return {
        "job": completed,
        "draft_id": draft_id,
        "request": request,
        "lease": lease,
        "completion": completion,
        "interrupted": old_lease,
    }


def verify_fictional_ocr_metadata(engine, ids):
    from coinpup_api.ocr.models import OcrDraft, OcrJob
    from coinpup_api.ocr.queue import OcrQueueService
    from sqlalchemy.orm import Session

    with Session(engine) as session:
        job = session.get(OcrJob, ids["job"].id)
        draft = session.get(OcrDraft, ids["draft_id"])
        if (
            job is None
            or draft is None
            or job.state != "succeeded"
            or job.attempts != 1
            or job.generation != 1
            or job.lease_token is not None
            or job.lease_until is not None
            or draft.job_id != job.id
            or draft.recognized["amount"] != "100.00"
            or draft.fields != {"amount": "99.00"}
            or draft.version != 2
        ):
            raise ArchiveError("Restored OCR metadata or independent human edits changed.")
    queue, lease = OcrQueueService(engine), ids["lease"]
    if (
        queue.create_job(lease.owner_id, lease.ledger_id, ids["request"], configuration=None)
        != ids["job"]
        or queue.finish(lease, ids["completion"]) != ids["job"]
    ):
        raise ArchiveError("Restored original queue intent or completion replay changed.")


def verify_restored_queue_continuation(engine, target, ids):
    from coinpup_api.ledger.service import LedgerError
    from coinpup_api.ocr.queue import OcrQueueService

    queue, old = OcrQueueService(engine), ids["interrupted"]
    fresh = queue.claim(old.owner_id)
    if (
        fresh is None
        or fresh.job_id != old.job_id
        or fresh.generation != old.generation + 1
        or fresh.token == old.token
    ):
        raise ArchiveError("Restored expired task did not acquire a fresh fenced lease.")
    before = snapshot(target)
    try:
        queue.finish(old, ids["completion"])
    except LedgerError as error:
        if error.code != "ocr_lease_lost":
            raise ArchiveError("Restored stale worker failed with an unexpected error.") from None
    else:
        raise ArchiveError("Restored stale worker was allowed to write candidates.")
    if snapshot(target) != before:
        raise ArchiveError("Restored stale worker rejection changed persisted rows.")
    completed = queue.finish(fresh, ids["completion"])
    after = snapshot(target)
    if (
        completed.result is None
        or len(completed.result.draft_ids) != 1
        or completed.result.draft_ids[0] == ids["draft_id"]
        or any(
            before[name] != rows
            for name, rows in after.items()
            if name not in {"ocr_jobs", "ocr_drafts", "change_log"}
        )
    ):
        raise ArchiveError("Restored queue completion changed non-OCR business rows.")
    verify_fictional_ocr_metadata(engine, ids)


def check_bundle_restore(source_url):
    """Extend, never replace, the already seeded database-only restoration exercise."""
    if os.environ.get("COINPUP_RUN_BACKUP_TESTS") != "1":
        raise ArchiveError("Set COINPUP_RUN_BACKUP_TESTS=1 only for disposable CI databases.")
    require_posix()
    from coinpup_api.files.schemas import FileUpdate, LinkUpdate, UploadCreate
    from coinpup_api.files.service import DocumentService
    from coinpup_api.files.storage import FileStore
    from coinpup_api.ledger.posting import PostingService
    from coinpup_api.ledger.posting_schemas import CancellationCreate, ExpenseCreate
    from coinpup_api.ledger.schemas import AccountCreate, EntityCreate, EntityUpdate
    from coinpup_api.ledger.service import LedgerError, LedgerService
    from psycopg import sql
    from sqlalchemy import create_engine

    source = read_target("COINPUP_BACKUP_TEST_SOURCE_URL")
    if source.database != "coinpup_backup_test" or source_url != os.environ.get(
        "COINPUP_BACKUP_TEST_SOURCE_URL"
    ):
        raise ArchiveError("Bundle exercise requires the existing disposable Compose fixture.")
    target = replace(source, database="coinpup_restore_bundle_test")
    with source.connect(autocommit=True) as connection:
        owners = connection.execute("SELECT id, username FROM administrators").fetchall()
        if len(owners) != 1 or owners[0][1] != "backup-ci-fixture":
            raise ArchiveError("Bundle exercise requires the synthetic backup administrator.")
        connection.execute(
            sql.SQL("CREATE DATABASE {} TEMPLATE template0").format(sql.Identifier(target.database))
        )
        owner = owners[0][0]
    engine = create_engine(source_url, hide_parameters=True)
    restored_url = engine.url.set(database=target.database)
    restored_engine = create_engine(restored_url, hide_parameters=True)
    try:
        with tempfile.TemporaryDirectory(prefix="coinpup-bundle-check-") as temporary:
            root = Path(temporary)
            storage, restored_storage, bundle = (
                root / "source-files",
                root / "restored-files",
                root / "bundle",
            )
            store = FileStore(storage, 1024 * 1024, 30)
            service, ledger_service, posting = (
                DocumentService(engine),
                LedgerService(engine),
                PostingService(engine),
            )
            personal = ledger_service.create_entity(
                owner,
                EntityCreate(
                    kind="personal",
                    name="Fictional bundle personal",
                    base_asset_id="USD",
                    template_key="personal_default",
                    locale="en",
                ),
            )
            company = ledger_service.create_entity(
                owner,
                EntityCreate(
                    kind="company",
                    name="Fictional bundle company",
                    country_code="US",
                    region_code="NM",
                    company_type="llc",
                    base_asset_id="USD",
                    template_key="business_default",
                    locale="en",
                ),
            )
            ledger_a, ledger_b = personal.ledger.id, company.ledger.id
            account = ledger_service.create_account(
                owner,
                ledger_a,
                AccountCreate(name="Fictional bundle cash", kind="cash", asset_ids=["USD"]),
            )
            category = next(
                item
                for item in ledger_service.list_categories(owner, ledger_a)
                if item.kind == "expense"
            )
            operation = posting.post_expense(
                owner,
                ledger_a,
                ExpenseCreate(
                    account_id=account.id,
                    asset_id="USD",
                    amount="100.00",
                    transaction_date=date(2026, 10, 3),
                    recognition_date=date(2026, 10, 3),
                    description="Fictional attachment association",
                    splits=[{"category_id": category.id, "amount": "100.00"}],
                ),
                str(uuid4()),
            )
            pdf = fictional_pdf()
            first = upload_fixture(
                service, store, owner, ledger_a, pdf, "Fictional receipt.pdf", operation.id
            )
            second = upload_fixture(
                service, store, owner, ledger_b, pdf, "Fictional company certificate.pdf"
            )
            picture = upload_fixture(service, store, owner, ledger_b, PNG, "Fictional receipt.png")
            confirmations = seed_confirmations(
                engine,
                owner,
                ledger_a,
                ledger_b,
                first["receipt"].file_id,
                second["receipt"].file_id,
                operation,
            )
            ocr_ids = fictional_ocr_metadata(engine, owner, ledger_b, second["receipt"].file_id)
            controls = seed_controls(engine, owner)
            periods = seed_periods(engine, owner)
            reminder = seed_reminders(engine, owner, ledger_a)
            duplicate = upload_fixture(
                service, store, owner, ledger_a, pdf, "Fictional duplicate.pdf", operation.id
            )
            if (
                not duplicate["receipt"].duplicate
                or duplicate["receipt"].file_id != first["receipt"].file_id
                or first["receipt"].file_id == second["receipt"].file_id
            ):
                raise ArchiveError(
                    "Fixture did not preserve same-ledger deduplication and cross-ledger isolation."
                )
            posting.cancel_operation(
                owner,
                ledger_a,
                operation.id,
                CancellationCreate(
                    expected_version=1, reason="Fictional cancelled attachment record"
                ),
                str(uuid4()),
            )
            service.update_link(
                owner,
                ledger_a,
                operation.id,
                first["receipt"].file_id,
                LinkUpdate(expected_version=1, archived=True),
            )
            service.update_file(
                owner,
                ledger_a,
                first["receipt"].file_id,
                FileUpdate(expected_version=1, archived=True, title="Fictional archived receipt"),
            )
            ledger_service.update_entity(
                owner, personal.id, EntityUpdate(expected_version=1, archived=True)
            )
            pending = UploadCreate(
                id=uuid4(), original_filename="Fictional unfinished.pdf", declared_size=len(pdf)
            )
            service.reserve_upload(owner, ledger_b, pending)
            # Both staged remnants and published-but-unreferenced blobs must stay out of the bundle.
            asyncio.run(stage_bytes(store, pdf))
            store.publish(asyncio.run(stage_bytes(store, fictional_pdf("Fictional orphan"))))
            engine.dispose()
            before = snapshot(source)
            expected_changes = change_state(engine, owner)
            import backup_bundle as implementation

            actual_run = implementation.run_tool
            late = []

            def concurrent_upload(arguments, **kwargs):
                if arguments[0] == "pg_dump":
                    late.append(
                        upload_fixture(
                            service,
                            store,
                            owner,
                            ledger_b,
                            fictional_pdf("Fictional after snapshot"),
                            "Fictional later.pdf",
                        )
                    )
                return actual_run(arguments, **kwargs)

            with patch.object(implementation, "run_tool", concurrent_upload):
                backup_bundle(source, storage, bundle)
            after = snapshot(source)
            if len(late) != 1 or after == before:
                raise ArchiveError(
                    "Concurrent-upload snapshot exercise did not execute its late write."
                )
            with verified_bundle(bundle) as (manifest, _dump, _blobs):
                if len(manifest["blobs"]) != 3 or manifest["stored_files"] != [
                    row[0]
                    for row in sorted(
                        before["stored_files"],
                        key=lambda row: json.loads(row[0])["id"],
                    )
                ]:
                    raise ArchiveError(
                        "Bundle included orphan, pending or post-snapshot file metadata."
                    )
            restore_bundle(target, bundle, restored_storage, target.database)
            if snapshot(target) != before or snapshot(source) != after:
                raise ArchiveError(
                    "Bundle restore differs from its exact snapshot or changed the source."
                )
            verify_fictional_ocr_metadata(restored_engine, ocr_ids)
            verify_confirmations(restored_engine, owner, ledger_a, confirmations)
            verify_controls(restored_engine, owner, controls)
            verify_periods(restored_engine, owner, periods)
            verify_reminders(restored_engine, reminder)
            restored_service = DocumentService(restored_engine)
            restored_store = FileStore(restored_storage, 1024 * 1024, 30)
            blob_names = set((restored_storage / "blobs").iterdir())
            for fixture in (first, second, picture, duplicate):
                file, key = restored_service.get_download(
                    owner, fixture["ledger"], fixture["receipt"].file_id
                )
                with restored_store.open_blob(key, file.sha256, file.byte_size) as source_file:
                    if source_file.read() != fixture["content"]:
                        raise ArchiveError(
                            "Restored private file bytes differ from the original upload."
                        )
                replay = upload_fixture(
                    restored_service,
                    restored_store,
                    owner,
                    fixture["ledger"],
                    fixture["content"],
                    fixture["request"].original_filename,
                    request=fixture["request"],
                )
                if replay["receipt"] != fixture["receipt"]:
                    raise ArchiveError("Restored upload replay changed its persisted receipt.")
            if restored_service.get_upload(owner, ledger_b, pending.id).state != "pending":
                raise ArchiveError("An unfinished upload became ready during restore.")
            try:
                restored_service.get_download(owner, ledger_b, first["receipt"].file_id)
            except LedgerError as error:
                if error.status != 404:
                    raise ArchiveError(
                        "Restored cross-ledger file read returned an unexpected response."
                    ) from None
            else:
                raise ArchiveError("Restored private attachment crossed its ledger boundary.")
            links = restored_service.list_operation_files(owner, ledger_a, operation.id)
            if (
                len(links) != 1
                or not links[0].archived
                or PostingService(restored_engine)
                .get_operation(owner, ledger_a, operation.id)
                .status
                != "cancelled"
            ):
                raise ArchiveError(
                    "Restored archived file association or cancelled operation changed."
                )
            if (
                snapshot(target) != before
                or set((restored_storage / "blobs").iterdir()) != blob_names
            ):
                raise ArchiveError("Restored upload replay created extra metadata or blobs.")
            restored_engine.dispose()
            try:
                restore_bundle(target, bundle, root / "second-restore-files", target.database)
            except ArchiveError as error:
                if "not empty" not in str(error):
                    raise
            else:
                raise ArchiveError("Bundle restore accepted a nonempty target database.")
            # Late writes can advance non-MVCC identity state beyond the exported snapshot.
            verify_change_continuation(restored_engine, target, owner, ledger_b, expected_changes)
            verify_restored_queue_continuation(restored_engine, target, ocr_ids)
            if snapshot(source) != after:
                raise ArchiveError("Restored cursor continuation changed the bundle source.")
            print(
                "Bundle restore verified: consistent PostgreSQL snapshot, "
                "exact private files and metadata."
            )
            print(
                "Concurrent late uploads, staging and orphan blobs excluded; "
                "archived reads and replay verified."
            )
            print("Isolated test databases retained; no DROP or production cutover ran.")
            print("OCR queue intent/completion replay and independent human edit restored exactly.")
            print(
                "Period close/reopen/reclose state, audits and original v2 receipts "
                "restored exactly."
            )
            print(
                "OCR five financial kinds and existing-operation link restored; original v2 "
                "intents/receipts replay after archive/cancellation without changing any table."
            )
            print(
                "Expired OCR task reclaimed with a fresh generation/token; stale worker rejected."
            )
            print(
                "Restored change cursor continues above visible rows and captured identity state."
            )
    finally:
        engine.dispose()
        restored_engine.dispose()
