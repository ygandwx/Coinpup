"""Simulate a fixed fictional worker outcome for real browser queue/retry acceptance only."""

import json
import os
from uuid import UUID

from coinpup_api.config import Settings
from coinpup_api.database import Database
from coinpup_api.files.models import StoredFile
from coinpup_api.ledger.models import Entity, Ledger
from coinpup_api.models import Administrator
from coinpup_api.ocr.models import OcrJob
from coinpup_api.ocr.queue import OcrQueueService
from create_ocr_browser_fixture import fictional_completion, require_fixture_target
from sqlalchemy import select
from sqlalchemy.orm import Session


def require_fictional_job(session, job_id):
    row = session.execute(
        select(OcrJob, StoredFile.original_filename, Entity.name)
        .join(StoredFile, StoredFile.id == OcrJob.file_id)
        .join(Ledger, Ledger.id == OcrJob.ledger_id)
        .join(Entity, Entity.id == Ledger.entity_id)
        .join(Administrator, Administrator.id == OcrJob.created_by)
        .where(OcrJob.id == job_id, Administrator.username == "admin-e2e")
    ).one_or_none()
    if (
        row is None
        or row[1] not in {"fictional-browser.pdf", "fictional-browser.png"}
        or not row[2].startswith("Fictional E2E OCR ")
    ):
        raise SystemExit("Requires a known fictional browser source owned by admin-e2e")
    return row[0].created_by


def main():
    settings = Settings()
    require_fixture_target(settings)
    job_id = UUID(os.environ["COINPUP_BROWSER_JOB_ID"])
    action = os.environ.get("COINPUP_BROWSER_JOB_ACTION")
    if action not in {"fail", "finish"}:
        raise SystemExit("Requires explicit fictional fail or finish action")
    database = Database(settings)
    try:
        with Session(database.engine) as session:
            owner = require_fictional_job(session, job_id)
        queue = OcrQueueService(database.engine)
        lease = queue.claim(owner)
        if lease is None or lease.job_id != job_id:
            raise SystemExit("Unexpected pending job in disposable browser database")
        result = (
            queue.fail(lease, "processor_unavailable")
            if action == "fail"
            else queue.finish(lease, fictional_completion(["10.00", "20.00", "30.00"]))
        )
        print(
            json.dumps(
                {
                    "job": str(result.id),
                    "state": result.state,
                    "drafts": [str(value) for value in result.result.draft_ids]
                    if result.result
                    else [],
                }
            )
        )
    finally:
        database.close()


if __name__ == "__main__":
    main()
