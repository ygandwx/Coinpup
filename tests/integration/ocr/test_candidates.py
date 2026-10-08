"""Real database persistence/replay of fictional parsed drafts, without an OCR SDK."""

from uuid import uuid4

import pytest
from coinpup_api.ocr.candidates import completion_from_result
from coinpup_api.ocr.contracts import JobCreate
from coinpup_api.ocr.models import OcrDraft, OcrJob
from coinpup_api.ocr.queue import OcrQueueService
from sqlalchemy import func, select, update

from tests.integration.ocr.test_ocr_queue import finances
from tests.integration.ocr.test_ocr_schema import ocr_structure as ocr_structure
from tests.integration.ocr.test_ocr_schema import writer
from tests.unit.ocr.test_candidates import statement

pytestmark = pytest.mark.integration


def test_original_result_persists_once_and_replay_preserves_human_fields(ocr_structure):
    s = ocr_structure
    queue = OcrQueueService(s["engine"])
    job = queue.create_job(
        s["owner"],
        s["ledgers"][0],
        JobCreate(intent_id=uuid4(), file_id=s["files"][0]),
        configuration={
            "lease_seconds": 120,
            "retry_seconds": 0,
            "processing": {"fixture": "Fictional mapper"},
        },
    )
    lease = queue.claim(s["owner"])
    assert lease.job_id == job.id
    original = statement("10.00", "10.00", "not-a-number", ocr=(0,))
    completion = completion_from_result(original)
    before = finances(s["engine"])
    receipt = queue.finish(lease, completion)
    assert receipt.result.summary == {"pages": 1, "manual_pages": 0, "candidates": 3}
    assert len(receipt.result.draft_ids) == 3
    assert "recognition" not in receipt.model_dump_json()
    with s["engine"].connect() as connection:
        stored = connection.scalar(select(OcrJob.result).where(OcrJob.id == job.id))
        assert stored["summary"]["recognition"] == original
        assert (
            connection.scalar(
                select(func.count()).select_from(OcrDraft).where(OcrDraft.job_id == job.id)
            )
            == 3
        )
    draft_id = receipt.result.draft_ids[0]
    edited = {"version": 1, "fixture": "Fictional explicit human edit", "amount": "11.00"}
    with writer(s) as session:
        session.execute(
            update(OcrDraft)
            .where(OcrDraft.id == draft_id)
            .values(
                version=2,
                updated_at=func.clock_timestamp(),
                fields=edited,
            )
        )
    assert queue.finish(lease, completion).model_dump() == receipt.model_dump()
    with s["engine"].connect() as connection:
        assert connection.scalar(select(OcrDraft.fields).where(OcrDraft.id == draft_id)) == edited
        assert connection.scalar(select(OcrJob.result).where(OcrJob.id == job.id)) == stored
        assert (
            connection.scalar(
                select(func.count()).select_from(OcrDraft).where(OcrDraft.job_id == job.id)
            )
            == 3
        )
    assert finances(s["engine"]) == before
