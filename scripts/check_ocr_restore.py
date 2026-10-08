"""Fictional real-worker bundle recovery, restricted to two disposable CI databases."""

import argparse
import os
from pathlib import Path
from uuid import uuid4

from _database_archive import read_target
from backup_bundle import backup_bundle
from check_backup_restore import change_state, snapshot
from check_ocr_worker import verify_result
from coinpup_api.config import Settings
from coinpup_api.database import Database
from coinpup_api.files.models import StoredFile
from coinpup_api.files.storage import FileStore
from coinpup_api.ledger.service import LedgerError, LedgerService, _touch
from coinpup_api.ocr.contracts import Completion, JobCreate
from coinpup_api.ocr.draft_reads import DraftReadService
from coinpup_api.ocr.models import OcrDraft, OcrJob
from coinpup_api.ocr.queue import OcrQueueService
from restore_bundle import restore_bundle
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

SOURCE_NAME = "coinpup_test_worker"
TARGET_NAME = "coinpup_restore_worker"
NOTE = "Fictional human review retained through worker recovery"


def targets():
    if os.environ.get("COINPUP_RUN_WORKER_RESTORE") != "1":
        raise ValueError("Requires explicit disposable worker restore opt-in")
    source = read_target("COINPUP_BACKUP_DATABASE_URL")
    target = read_target("COINPUP_RESTORE_DATABASE_URL")
    if (source.database, target.database) != (SOURCE_NAME, TARGET_NAME):
        raise ValueError("Requires the exact disposable worker database pair")
    return source, target


def database(variable):
    return Database(Settings(_env_file=None, database_url=os.environ[variable]))


def prepare(engine):
    with Session(engine) as session:
        jobs = session.scalars(select(OcrJob)).all()
        assert len(jobs) == 2 and all(job.state == "succeeded" for job in jobs)
        job = session.scalar(
            select(OcrJob)
            .join(StoredFile, StoredFile.id == OcrJob.file_id)
            .where(StoredFile.detected_media_type == "application/pdf")
        )
        owner, ledger, file_id, config = (
            job.created_by,
            job.ledger_id,
            job.file_id,
            job.configuration,
        )
    queue = OcrQueueService(engine)
    created = queue.create_job(
        owner, ledger, JobCreate(intent_id=uuid4(), file_id=file_id), configuration=config
    )
    lease = queue.claim(owner)
    assert lease is not None and lease.job_id == created.id
    scope = LedgerService(engine)
    with scope._transaction(owner, write=True) as session:
        scope._ledger(session, owner, ledger, write=True)
        interrupted = session.get(OcrJob, lease.job_id, with_for_update=True)
        interrupted.lease_until = func.clock_timestamp() - text("INTERVAL '1 second'")
        _touch(interrupted)
        draft = session.scalar(select(OcrDraft).order_by(OcrDraft.id).with_for_update())
        draft.fields = {**draft.fields, "fictional_review_note": NOTE}
        _touch(draft)
    print("Fictional interrupted lease and versioned human review prepared.")


def verify_blobs(engine, root):
    store = FileStore(root, 50 * 1024**2, 30)
    with Session(engine) as session:
        files = session.scalars(select(StoredFile)).all()
        assert len(files) == 2
        for file in files:
            with store.open_blob(file.blob_key, file.sha256, file.byte_size) as stream:
                assert len(stream.read(file.byte_size + 1)) == file.byte_size


def bundle(source, target, engine, root):
    before = snapshot(source)
    backup_bundle(source, Path("/source"), root / "bundle")
    restore_bundle(target, root / "bundle", root / "restored", TARGET_NAME)
    assert snapshot(source) == before, "Backup/restore changed source rows"
    assert snapshot(target) == before, "Restored tables differ from the complete snapshot"
    verify_blobs(engine, root / "restored")
    print("Real bundle restored every table and both SHA-verified originals into an empty target.")


def verify(source, target, source_engine, target_engine, root):
    before, original = snapshot(target), snapshot(source)
    with Session(source_engine) as session:
        interrupted = session.scalar(select(OcrJob).where(OcrJob.state == "running"))
        assert interrupted is not None
        file = session.get(StoredFile, interrupted.file_id)
        old = OcrQueueService._lease(interrupted, file)
        old_version, attempts = interrupted.version, interrupted.attempts
        replay_requests = [
            (job.id, JobCreate(intent_id=job.intent_id, file_id=job.file_id))
            for job in session.scalars(select(OcrJob).where(OcrJob.state == "succeeded"))
        ]
    queue = OcrQueueService(target_engine)
    with Session(target_engine) as session:
        jobs = session.scalars(select(OcrJob)).all()
        assert len(jobs) == 3 and all(job.state == "succeeded" for job in jobs)
        resumed = session.get(OcrJob, old.job_id)
        assert resumed.generation == old.generation + 1 and resumed.attempts == attempts + 1
        assert resumed.version > old_version and resumed.lease_token != old.token
        verify_result(resumed.result["summary"]["recognition"], image=False)
        drafts = session.scalars(select(OcrDraft)).all()
        assert len(drafts) == 3
        assert sum(d.fields.get("fictional_review_note") == NOTE for d in drafts) == 1
    # No rows may change on either rejected stale attempt, including the change log.
    for action in (
        lambda: queue.renew(old),
        lambda: queue.finish(old, Completion(summary={}, candidates=())),
    ):
        try:
            action()
        except LedgerError as error:
            assert error.code == "ocr_lease_lost"
        else:
            raise AssertionError("Restored worker accepted a stale pre-backup lease")
        assert snapshot(target) == before
    assert original.keys() == before.keys()
    for name, rows in original.items():
        if name not in {"ocr_jobs", "ocr_drafts", "change_log"}:
            assert before[name] == rows, "Worker changed non-OCR rows"
    assert set(original["ocr_drafts"]) <= set(before["ocr_drafts"])
    # The two previously completed jobs, including original evidence, remain byte-identical.
    assert len(set(original["ocr_jobs"]) & set(before["ocr_jobs"])) == 2
    for job_id, request in replay_requests:
        expected = OcrQueueService(source_engine).get_job(old.owner_id, old.ledger_id, job_id)
        replay = queue.create_job(old.owner_id, old.ledger_id, request, configuration={})
        assert replay == expected, "Restored recognition intent did not replay its original result"
    old_changes, cursor = change_state(source_engine, old.owner_id)
    new_changes, new_cursor = change_state(target_engine, old.owner_id)
    assert new_changes[: len(old_changes)] == old_changes and int(new_cursor) > int(cursor)
    assert all(
        c["entity_type"] in {"ocr_jobs", "ocr_drafts"} for c in new_changes[len(old_changes) :]
    )
    reads = DraftReadService(target_engine)
    for draft in reads.list_drafts(old.owner_id, old.ledger_id):
        detail = reads.get_draft(old.owner_id, old.ledger_id, draft.id)
        assert detail.recognition and detail.selection
    verify_blobs(target_engine, root / "restored")
    assert snapshot(target) == before and snapshot(source) == original
    print(
        "Real restored worker reclaimed the lease, retained edits/evidence, "
        "rejected stale writes, and preserved finances."
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("prepare", "bundle", "verify"))
    args = parser.parse_args()
    source, target = targets()  # Before opening either engine or touching private files.
    root = Path("/app/data/files")
    source_db = database("COINPUP_BACKUP_DATABASE_URL")
    target_db = database("COINPUP_RESTORE_DATABASE_URL")
    try:
        if args.action == "prepare":
            prepare(source_db.engine)
        elif args.action == "bundle":
            bundle(source, target, target_db.engine, root)
        else:
            verify(source, target, source_db.engine, target_db.engine, root)
    finally:
        source_db.close()
        target_db.close()


if __name__ == "__main__":
    main()
