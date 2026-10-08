"""Explicit fictional UI candidates, not an OCR quality test or application startup hook."""

import base64
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

# A generated, valid PNG that visibly says Fictional preview only.
PHOTO = base64.b64decode(
    "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"
)


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
        photo = os.environ.get("COINPUP_BROWSER_FIXTURE_IMAGE") == "1"
        uploaded = upload_fixture(
            DocumentService(database.engine),
            FileStore(
                settings.files_directory, settings.max_upload_bytes, settings.upload_timeout_seconds
            ),
            owner,
            ledger,
            PHOTO
            if photo
            else fictional_pdf(
                "Fictional total USD 10.00", pages=2, identity=f"Fictional {entity.id}"
            ),
            "fictional-browser.png" if photo else "fictional-browser.pdf",
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
