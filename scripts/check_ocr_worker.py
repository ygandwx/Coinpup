"""Explicitly fictional container smoke checks, not OCR quality acceptance or tuning."""

import argparse
import asyncio
import hashlib
import json
import os
import secrets
import tempfile
from io import BytesIO
from pathlib import Path
from uuid import uuid4

from coinpup_api.admin import create_admin
from coinpup_api.config import Settings
from coinpup_api.database import Database
from coinpup_api.files.schemas import UploadCreate
from coinpup_api.files.service import DocumentService
from coinpup_api.files.storage import FileStore
from coinpup_api.ledger.models import FinancialOperation
from coinpup_api.ledger.schemas import EntityCreate
from coinpup_api.ledger.service import LedgerService
from coinpup_api.models import Administrator
from coinpup_api.ocr.contracts import JobCreate
from coinpup_api.ocr.isolation import ProcessBudget, run_isolated
from coinpup_api.ocr.models import OcrDraft, OcrJob
from coinpup_api.ocr.queue import OcrQueueService
from coinpup_api.ocr.runtime import processing_configuration
from sqlalchemy import func, select


def samples():
    from PIL import Image, ImageDraw, ImageFont
    from pypdf import PageObject, PdfWriter
    from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

    with PdfWriter() as writer, BytesIO() as output:
        page = PageObject.create_blank_page(width=500, height=200)
        page[NameObject("/Resources")] = DictionaryObject(
            {
                NameObject("/Font"): DictionaryObject(
                    {
                        NameObject("/F1"): DictionaryObject(
                            {
                                NameObject("/Type"): NameObject("/Font"),
                                NameObject("/Subtype"): NameObject("/Type1"),
                                NameObject("/BaseFont"): NameObject("/Helvetica"),
                            }
                        ),
                    }
                )
            }
        )
        stream = DecodedStreamObject()
        stream.set_data(b"BT /F1 24 Tf 20 100 Td (Total: 12.00) Tj ET")
        page[NameObject("/Contents")] = stream
        writer.add_page(page)
        writer.add_metadata({"/Title": "FICTIONAL WORKER SMOKE"})
        writer.write(output)
        pdf = output.getvalue()
    with Image.new("RGB", (1000, 300), "white") as image, BytesIO() as output:
        ImageDraw.Draw(image).multiline_text(
            (30, 25),
            "FICTIONAL WORKER CHECK\nDate: 2031-07-18\nCurrency: USD\nTotal: 12.00",
            font=ImageFont.load_default(size=36),
            fill="black",
            spacing=14,
        )
        image.save(output, format="PNG")
        png = output.getvalue()
    return (("pdf", "application/pdf", pdf), ("png", "image/png", png))


def verify_result(result, *, image):
    assert result["status"] == "processed", (
        "Fictional recognition did not complete",
        result["status"],
        result.get("reason"),
    )
    assert result["raw_text"].strip(), "Fictional source text missing"
    fields = result["field_review"]
    assert fields, "Fictional recognition did not produce review fields"
    if image:
        assert result["timings_ns"]["recognize"] > 0
        assert all(
            f["source"] == "ocr" and f["requires_confirmation"] and f["suggested_value"] is None
            for f in fields
        )
    else:
        assert result["timings_ns"]["recognize"] == 0
        assert any(f["suggested_value"] == "12.00" and f["source"] == "text" for f in fields)


def offline():
    processing = processing_configuration()
    with tempfile.TemporaryDirectory(prefix="fictional-worker-check-") as name:
        directory = Path(name)
        directory.chmod(0o700)
        for suffix, media, data in samples():
            path = directory / f"{uuid4().hex}.{suffix}"
            path.write_bytes(data)
            path.chmod(0o600)
            result = run_isolated(
                {
                    "version": 1,
                    "action": "recognize_document",
                    "media_type": media,
                    "source": {
                        "path": str(path),
                        "sha256": hashlib.sha256(data).hexdigest(),
                        "byte_size": len(data),
                    },
                    "processing": processing,
                },
                ProcessBudget(**processing["process"]),
            )
            verify_result(json.loads(result.output), image=suffix == "png")
    print("Offline fixed text/OCR calls passed; text prefill and OCR mandatory review verified.")


def database():
    settings = Settings(_env_file=None)
    from sqlalchemy.engine import make_url

    if (
        os.environ.get("COINPUP_RUN_WORKER_SMOKE") != "1"
        or make_url(settings.database_url.get_secret_value()).database != "coinpup_test_worker"
    ):
        raise ValueError("Requires an explicitly opted-in disposable worker database")
    return settings, Database(settings)


def seed(settings, engine):
    with engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(Administrator)) == 0, (
            "Refuse nonempty administrator database"
        )
    owner = create_admin(engine, "fictional-worker-check", secrets.token_urlsafe(24))
    ledger = (
        LedgerService(engine)
        .create_entity(
            owner,
            EntityCreate(kind="personal", name="Fictional worker ledger", base_asset_id="USD"),
        )
        .ledger.id
    )
    files = DocumentService(engine)
    store = FileStore(
        settings.files_directory, settings.max_upload_bytes, settings.upload_timeout_seconds
    )
    queue = OcrQueueService(engine)
    for suffix, _media, data in samples():
        upload = UploadCreate(
            id=uuid4(), original_filename=f"fictional-worker.{suffix}", declared_size=len(data)
        )
        files.reserve_upload(owner, ledger, upload)

        async def stage(data=data):
            async def chunks():
                yield data

            return await store.stage(chunks(), len(data))

        staged = asyncio.run(stage())
        try:
            key = store.publish(staged)
            ready = files.finalize_upload(owner, ledger, upload.id, staged, key)
        finally:
            store.discard(staged)
        queue.create_job(
            owner,
            ledger,
            JobCreate(intent_id=uuid4(), file_id=ready.file_id),
            configuration={
                "lease_seconds": 120,
                "retry_seconds": 30,
                "processing": processing_configuration(),
            },
        )
    print("Two new fictional worker jobs seeded; no financial commands executed.")


def verify(engine):
    with engine.connect() as connection:
        jobs = connection.execute(select(OcrJob.state, OcrJob.result)).all()
        assert len(jobs) == 2 and all(job.state == "succeeded" for job in jobs), (
            "Fictional worker jobs incomplete"
        )
        images = []
        for job in jobs:
            result = job.result["summary"]["recognition"]
            image = result["pages"][0]["layer"] == "absent"
            verify_result(result, image=image)
            images.append(image)
        assert sorted(images) == [False, True]
        assert connection.scalar(select(func.count()).select_from(OcrDraft)) == 2
        assert connection.scalar(select(func.count()).select_from(FinancialOperation)) == 0
    print(
        "Real worker persisted both fictional drafts with original evidence and no financial rows."
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("offline", "seed", "verify"))
    action = parser.parse_args().action
    if action == "offline":
        offline()
        return
    settings, db = database()
    try:
        seed(settings, db.engine) if action == "seed" else verify(db.engine)
    finally:
        db.close()


if __name__ == "__main__":
    main()
