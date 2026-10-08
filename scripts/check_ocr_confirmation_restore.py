"""Fictional confirmations for the guarded, isolated database/private-file bundle check."""

import json
from uuid import uuid4

from _database_archive import ArchiveError
from coinpup_api.ledger.posting import PostingService
from coinpup_api.ledger.posting_schemas import CancellationCreate
from coinpup_api.ledger.schemas import AccountCreate
from coinpup_api.ledger.service import LedgerError, LedgerService
from coinpup_api.ocr.confirmation import ConfirmationService
from coinpup_api.ocr.confirmation_contracts import ConfirmationCreate
from coinpup_api.ocr.confirmation_reads import ConfirmationReadService
from coinpup_api.ocr.contracts import Candidate, Completion, JobCreate
from coinpup_api.ocr.queue import OcrQueueService
from coinpup_api.ocr.review import DraftReviewService, DraftReviewUpdate, HumanReview

KINDS = ("opening", "income", "expense", "transfer", "exchange", "link")


def fictional_completion():
    fields = [
        {
            "path": "header.total",
            "role": "total",
            "value": "10.00",
            "status": "certain",
            "evidence": [{"page": 0, "raw": "Fictional USD 10.00"}],
        }
    ]
    return Completion(
        summary={
            "pages": 1,
            "recognition": {
                "raw_text": "Fictional service fixture, not an OCR quality sample",
                "pages": [{"page_index": 0, "layer": "absent", "route": "render"}],
                "parsed": {"fields": fields},
            },
        },
        candidates=[
            Candidate(
                source_key=f"fictional-restore:{kind}",
                recognized={"version": 1, "kind": "document", "fields": fields},
                evidence={"pages": [0]},
                fields={},
            )
            for kind in KINDS
        ],
    )


def seed_confirmations(engine, owner, source_ledger, target_ledger, source_file, target_file, link):
    """Capture original bytes before posting; never rebuild an intent from normalized receipts."""
    structure, queue = LedgerService(engine), OcrQueueService(engine)
    accounts = [
        structure.create_account(
            owner,
            target_ledger,
            AccountCreate(
                name=f"Fictional restored OCR {name}", kind="bank", asset_ids=["USD", "EUR"]
            ),
        )
        for name in ("source", "destination")
    ]
    categories = {item.kind: item.id for item in structure.list_categories(owner, target_ledger)}
    created = queue.create_job(
        owner,
        source_ledger,
        JobCreate(intent_id=uuid4(), file_id=source_file),
        configuration={"lease_seconds": 300, "retry_seconds": 0, "processing": {}},
    )
    lease = queue.claim(owner)
    if lease is None or lease.job_id != created.id:
        raise ArchiveError("Fictional confirmation fixture did not acquire its exact task.")
    completed = queue.finish(lease, fictional_completion())
    confirmer, reviews = ConfirmationService(engine), DraftReviewService(engine)
    records = []
    for kind, draft in zip(KINDS, completed.result.draft_ids, strict=True):
        command = {"id": str(uuid4()), "transaction_date": "2031-07-18"}
        if kind in {"opening", "income", "expense"}:
            command.update(account_id=str(accounts[0].id), asset_id="USD", amount="10.00")
            if kind != "opening":
                command.update(
                    recognition_date="2031-07-17",
                    splits=[{"category_id": str(categories[kind]), "amount": "10.00"}],
                )
        else:
            command.update(
                source_account_id=str(accounts[0].id), destination_account_id=str(accounts[1].id)
            )
            if kind == "transfer":
                command.update(asset_id="USD", amount="1.00")
            else:
                command.update(
                    source_asset_id="USD",
                    source_amount="1.00",
                    destination_asset_id="EUR",
                    destination_amount="0.90",
                )
        entry = (
            {"kind": "link", "operation_id": str(link.id), "expected_version": 1}
            if kind == "link"
            else {"kind": kind, "command": command}
        )
        reviewed = reviews.update_review(
            owner,
            source_ledger,
            draft,
            DraftReviewUpdate(
                expected_version=1, review=HumanReview(confirmed=["header.total"], entry=entry)
            ),
        )
        original = {
            "intent_id": str(uuid4()),
            "expected_version": reviewed.version,
            "target_ledger_id": str(source_ledger if kind == "link" else target_ledger),
            "target_file_id": str(source_file if kind == "link" else target_file),
            "confirmed": ["header.total"],
            "entry": entry,
        }
        raw = json.dumps(original, ensure_ascii=False, separators=(",", ":")).encode()
        receipt = confirmer.confirm(
            owner, source_ledger, draft, ConfirmationCreate.model_validate_json(raw), raw_body=raw
        )
        records.append(
            (
                draft,
                raw,
                receipt.model_dump_json(),
                reviews.get_review(owner, source_ledger, draft).model_dump_json(),
            )
        )
        if kind == "expense":
            PostingService(engine).cancel_operation(
                owner,
                target_ledger,
                receipt.operation.id,
                CancellationCreate(
                    expected_version=1, reason="Fictional cancellation after OCR confirmation"
                ),
                str(uuid4()),
            )
    return records


def verify_confirmations(engine, owner, ledger, records):
    confirmer, reviews, reads = (
        ConfirmationService(engine),
        DraftReviewService(engine),
        ConfirmationReadService(engine),
    )
    for draft, raw, expected, review in records:
        receipt = confirmer.confirm(
            owner, ledger, draft, ConfirmationCreate.model_validate_json(raw), raw_body=raw
        )
        if (
            receipt.model_dump_json() != expected
            or reads.get_confirmation(owner, ledger, draft).model_dump_json() != expected
        ):
            raise ArchiveError("Restored OCR confirmation changed its permanent original receipt.")
        if reviews.get_review(owner, ledger, draft).model_dump_json() != review:
            raise ArchiveError("Restored OCR human review or original field authority changed.")
        changed = json.loads(raw)
        if changed["entry"]["kind"] != "link":
            changed["entry"]["command"]["description"] = ""
            changed_raw = json.dumps(changed).encode()
            try:
                confirmer.confirm(
                    owner,
                    ledger,
                    draft,
                    ConfirmationCreate.model_validate_json(changed_raw),
                    raw_body=changed_raw,
                )
            except LedgerError as error:
                if error.code != "idempotency_conflict":
                    raise ArchiveError(
                        "Restored OCR v2 intent returned the wrong conflict."
                    ) from None
            else:
                raise ArchiveError("Restored OCR v2 accepted a changed original intent.")
