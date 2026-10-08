"""Actual PG/private-store worker lifecycle; the injected recognizer is explicitly fake."""

import json
from pathlib import Path
from uuid import uuid4

import pytest
from coinpup_api.files.schemas import FileUpdate
from coinpup_api.ocr.contracts import JobCreate
from coinpup_api.ocr.isolation import IsolationError, ProcessResult
from coinpup_api.ocr.models import OcrDraft, OcrJob
from coinpup_api.ocr.queue import OcrQueueService
from coinpup_api.ocr.runtime import processing_configuration
from coinpup_api.ocr.worker import run_once
from coinpup_api.sync.locking import acquire_write_lock
from sqlalchemy import func, select, text, update
from sqlalchemy.orm import Session

from tests.integration.files.test_files_service import PDF, complete, reserve
from tests.integration.files.test_files_service import setup as setup
from tests.integration.ocr.test_ocr_queue import finances
from tests.unit.ocr.test_candidates import statement

pytestmark = pytest.mark.integration


@pytest.fixture
def worker_setup(setup):
    s = setup
    upload, _ = reserve(s)
    s["file"] = complete(s, upload).file_id
    s["queue"] = OcrQueueService(s["engine"])
    s["job"] = s["queue"].create_job(
        s["owner"],
        s["ledger"],
        JobCreate(intent_id=uuid4(), file_id=s["file"]),
        configuration={
            "lease_seconds": 120,
            "retry_seconds": 0,
            "processing": processing_configuration(),
        },
    )
    return s


@pytest.mark.parametrize("interruption", [None, "archive", "expire"])
def test_worker_releases_locks_during_compute_and_fences_late_results(worker_setup, interruption):
    s, paths = worker_setup, []
    financial = finances(s["engine"])
    result = statement("10.00", "20.00", "bad", ocr=(0,))

    def recognize(request, budget, *, heartbeat):
        paths.append(Path(request["source"]["path"]))
        assert paths[0].read_bytes() == PDF
        assert s["engine"].pool.checkedout() == 0
        # A separate actual connection can acquire every write lock during computation.
        with Session(s["engine"]) as session, session.begin():
            session.execute(text("SET LOCAL lock_timeout='1000ms'"))
            acquire_write_lock(session)
            s["structure"]._ledger(session, s["owner"], s["ledger"], write=True)
        old = s["queue"].get_job(s["owner"], s["ledger"], s["job"].id)
        assert heartbeat(force=True)
        renewed = s["queue"].get_job(s["owner"], s["ledger"], s["job"].id)
        assert renewed.version > old.version and renewed.state == "running"
        assert s["engine"].pool.checkedout() == 0
        if interruption == "archive":
            s["service"].update_file(
                s["owner"], s["ledger"], s["file"], FileUpdate(expected_version=1, archived=True)
            )
            assert not heartbeat(force=True)
            raise IsolationError("processing_cancelled")
        if interruption == "expire":
            with s["structure"]._transaction(s["owner"], write=True) as session:
                s["structure"]._ledger(session, s["owner"], s["ledger"], write=True)
                session.execute(
                    update(OcrJob)
                    .where(OcrJob.id == s["job"].id)
                    .values(
                        lease_until=func.clock_timestamp() - text("INTERVAL '1 second'"),
                        version=renewed.version + 1,
                        updated_at=func.clock_timestamp(),
                    )
                )
        return ProcessResult(json.dumps(result).encode(), 0, 1)

    state = run_once(s["queue"], s["store"], s["owner"], isolate=recognize)
    assert state == ("succeeded" if interruption is None else "cancelled")
    assert paths and not paths[0].parent.exists()
    assert finances(s["engine"]) == financial
    with s["engine"].connect() as connection:
        count = connection.scalar(
            select(func.count()).select_from(OcrDraft).where(OcrDraft.job_id == s["job"].id)
        )
        assert count == (3 if interruption is None else 0)
        stored = connection.scalar(select(OcrJob.result).where(OcrJob.id == s["job"].id))
        if interruption is None:
            assert stored["summary"]["recognition"] == result
        else:
            assert stored is None


def test_missing_private_original_fails_without_invoking_recognizer(worker_setup, tmp_path):
    from coinpup_api.files.storage import FileStore

    s = worker_setup
    absent = FileStore(tmp_path / "fictional-empty-store", 1024, 5)
    state = run_once(
        s["queue"],
        absent,
        s["owner"],
        isolate=lambda *_args, **_kwargs: pytest.fail("Missing source reached OCR"),
    )
    assert state == "failed"
    job = s["queue"].get_job(s["owner"], s["ledger"], s["job"].id)
    assert job.error_code == "source_unavailable" and job.result is None
