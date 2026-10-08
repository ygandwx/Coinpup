"""Explicit fictional UI candidates, not an OCR quality test or application startup hook."""

import json
import os
from uuid import uuid4

from check_bundle_restore import fictional_pdf, upload_fixture
from coinpup_api.config import Settings
from coinpup_api.database import Database
from coinpup_api.files.service import DocumentService
from coinpup_api.files.storage import FileStore
from coinpup_api.ledger.schemas import EntityCreate
from coinpup_api.ledger.service import LedgerService
from coinpup_api.models import Administrator
from coinpup_api.ocr.contracts import Candidate, Completion, JobCreate
from coinpup_api.ocr.queue import OcrQueueService
from coinpup_api.ocr.runtime import field_review
from sqlalchemy import select
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session


def require_fixture_target(settings):
    url = make_url(settings.database_url.get_secret_value())
    if (
        os.environ.get("COINPUP_CREATE_OCR_BROWSER_FIXTURE") != "1"
        or settings.environment != "test"
        or (url.host, url.database) != ("postgres", "coinpup")
    ):
        raise SystemExit("Requires explicit disposable Compose OCR browser opt-in")


def main():
    settings = Settings()
    require_fixture_target(settings)
    database = Database(settings)
    try:
        with Session(database.engine) as session:
            owner = session.scalar(
                select(Administrator.id).where(Administrator.username == "admin-e2e")
            )
        if owner is None:
            raise SystemExit("Requires the fictional admin-e2e administrator")
        entity = LedgerService(database.engine).create_entity(
            owner,
            EntityCreate(
                kind="personal",
                name=f"Fictional E2E OCR {uuid4().hex[:8]}",
                base_asset_id="USD",
            ),
        )
        ledger = entity.ledger.id
        uploaded = upload_fixture(
            DocumentService(database.engine),
            FileStore(
                settings.files_directory, settings.max_upload_bytes, settings.upload_timeout_seconds
            ),
            owner,
            ledger,
            fictional_pdf("Fictional browser receipt - Total USD 10.00"),
            "fictional-browser.pdf",
        )
        file_id = uploaded["receipt"].file_id
        fields = [
            {
                "path": f"header.{role}",
                "role": role,
                "value": value,
                "status": "certain",
                "evidence": [{"page": 0, "raw": value}],
            }
            for role, value in [
                ("total", "10.00"),
                ("currency", "USD"),
                ("document_date", "2031-07-18"),
            ]
        ]
        recognition = {
            "raw_text": "Fictional seeded browser candidate: Total USD 10.00",
            "pages": [{"page_index": 0, "layer": "absent", "route": "render"}],
            "parsed": {"fields": fields},
        }
        queue = OcrQueueService(database.engine)
        job = queue.create_job(
            owner,
            ledger,
            JobCreate(intent_id=uuid4(), file_id=file_id),
            configuration={
                "lease_seconds": 120,
                "retry_seconds": 0,
                "processing": {"fixture": "Fictional browser seed; no OCR ran"},
            },
        )
        lease = queue.claim(owner)
        if lease is None or lease.job_id != job.id:
            raise SystemExit("Unexpected pending job in disposable browser database")
        done = queue.finish(
            lease,
            Completion(
                summary={"pages": 1, "recognition": recognition},
                candidates=[
                    Candidate(
                        source_key="document:0",
                        recognized={"version": 1, "kind": "document", "fields": fields},
                        evidence={"pages": [0]},
                        fields={"review": field_review(recognition), "confirmed": []},
                    )
                ],
            ),
        )
        print(
            json.dumps(
                {
                    "entity": str(entity.id),
                    "ledger": str(ledger),
                    "job": str(job.id),
                    "draft": str(done.result.draft_ids[0]),
                }
            )
        )
    finally:
        database.close()


if __name__ == "__main__":
    main()
