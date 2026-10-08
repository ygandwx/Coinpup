"""Atomic human confirmation with permanent original-intent replay and existing postings."""

from uuid import uuid4

from pydantic import ValidationError
from sqlalchemy import select

from coinpup_api.files.models import StoredFile
from coinpup_api.ledger.models import FinancialOperation
from coinpup_api.ledger.readers import PostingReaders
from coinpup_api.ledger.service import LedgerError, LedgerService, _not_found, _touch, _version

from .confirmation_contracts import ConfirmationResponse, command_assets, confirmation_request
from .contracts import CANDIDATE_BYTES, canonical_json
from .models import OcrConfirmation
from .review import HumanReview, reviewed_fields, scoped_draft
from .transactions import bind_commands


def duplicate_confirmations(session, owner_id, draft, source):
    return session.scalars(
        select(OcrConfirmation)
        .join(StoredFile, StoredFile.id == OcrConfirmation.source_file_id)
        .where(
            OcrConfirmation.created_by == owner_id,
            OcrConfirmation.draft_id != draft.id,
            OcrConfirmation.source_key == draft.source_key,
            StoredFile.sha256 == source.sha256,
            StoredFile.byte_size == source.byte_size,
            StoredFile.detected_media_type == source.detected_media_type,
        )
        .order_by(OcrConfirmation.created_at, OcrConfirmation.id)
        .limit(20)
    ).all()


def source_files(session, owner_id, ledger_id, job, payload):
    found = []
    for file_id, scope in [
        (job.file_id, ledger_id),
        (payload.target_file_id, payload.target_ledger_id),
    ]:
        file = session.scalar(
            select(StoredFile).where(
                StoredFile.id == file_id,
                StoredFile.ledger_id == scope,
                StoredFile.created_by == owner_id,
            )
        )
        if file is None:
            raise _not_found()
        if file.archived:
            raise LedgerError("file_archived", 409, "Restore the original before confirming.")
        found.append(file)
    source, target = found
    if (source.sha256, source.byte_size, source.detected_media_type) != (
        target.sha256,
        target.byte_size,
        target.detected_media_type,
    ):
        raise LedgerError("ocr_source_mismatch", 409, "The target file is not the same original.")
    return source, target


def replay(receipt, digest):
    if receipt.hash_version != 2:
        raise LedgerError("ocr_invalid_receipt", 503, "The confirmation receipt is inconsistent.")
    if receipt.request_hash != digest:
        raise LedgerError("idempotency_conflict", 409, "This intent was used for another request.")
    try:
        response = ConfirmationResponse.model_validate(receipt.response)
        if (
            response.intent_id != receipt.intent_id
            or response.draft_id != receipt.draft_id
            or response.ledger_id != receipt.ledger_id
            or response.target_ledger_id != receipt.target_ledger_id
            or response.target_file_id != receipt.target_file_id
            or response.operation.id != receipt.operation_id
            or response.operation.ledger_id != receipt.target_ledger_id
            or response.operation.version != receipt.operation_version
            or response.draft_version != receipt.draft_version + 1
            or response.action != receipt.action
        ):
            raise ValueError
        return response
    except (ValidationError, ValueError):
        raise LedgerError(
            "ocr_invalid_receipt", 503, "The confirmation receipt is inconsistent."
        ) from None


class ConfirmationService(LedgerService):
    def confirm(self, owner_id, ledger_id, draft_id, payload, *, raw_body=None):
        original, digest = confirmation_request(ledger_id, draft_id, payload, raw_body)
        with self._transaction(owner_id, write=True) as session:
            self._ledger(
                session, owner_id, ledger_id
            )  # Permission only, before business row locks.
            receipt = session.scalar(
                select(OcrConfirmation).where(
                    OcrConfirmation.ledger_id == ledger_id,
                    OcrConfirmation.intent_id == payload.intent_id,
                    OcrConfirmation.created_by == owner_id,
                )
            )
            if receipt is not None:
                return replay(receipt, digest)
            bound = bind_commands(
                session,
                owner_id,
                [ledger_id, payload.target_ledger_id],
                command_assets(payload.entry),
            )
            # These re-enter only already-held locks; state checks follow permanent replay.
            for scope in sorted({ledger_id, payload.target_ledger_id}, key=str):
                self._ledger(session, owner_id, scope, write=True)
            draft, job = scoped_draft(session, owner_id, ledger_id, draft_id, write=True)
            _version(draft, payload.expected_version)
            if draft.status != "draft":
                raise LedgerError(
                    "ocr_draft_not_editable", 409, "Restore an unconfirmed draft before posting."
                )
            if job.state != "succeeded":
                raise LedgerError("ocr_job_incomplete", 409, "Recognition has not completed.")
            fields = reviewed_fields(
                draft,
                job,
                HumanReview(
                    confirmed=payload.confirmed,
                    entry=original["entry"],
                ),
                complete=True,
            )
            source, target = source_files(session, owner_id, ledger_id, job, payload)
            if (
                duplicate_confirmations(session, owner_id, draft, source)
                and not payload.duplicate_ack
            ):
                raise LedgerError(
                    "ocr_duplicate_confirmation",
                    409,
                    "This source was confirmed before; review the duplicate warning.",
                )
            entry = payload.entry
            if entry.kind == "link":
                operation = session.scalar(
                    select(FinancialOperation)
                    .where(
                        FinancialOperation.id == entry.operation_id,
                        FinancialOperation.ledger_id == payload.target_ledger_id,
                    )
                    .with_for_update()
                )
                if operation is None:
                    raise _not_found()
                _version(operation, entry.expected_version)
                if operation.status != "active":
                    raise LedgerError(
                        "operation_cancelled", 409, "Choose an active financial operation."
                    )
                financial = PostingReaders._read_operation(session, operation)
            else:
                financial = bound.post(
                    entry.kind,
                    payload.target_ledger_id,
                    entry.command,
                    f"ocr:{ledger_id.hex}:{payload.intent_id.hex}",
                    raw_body=canonical_json(original["entry"]["command"], CANDIDATE_BYTES).encode(
                        "utf-8"
                    ),
                )
            link = bound.link(payload.target_ledger_id, financial.id, target.id)
            if link.archived:
                raise LedgerError(
                    "file_link_archived",
                    409,
                    "Restore the existing evidence link before confirming.",
                )
            response = ConfirmationResponse(
                intent_id=payload.intent_id,
                draft_id=draft.id,
                ledger_id=ledger_id,
                draft_version=draft.version + 1,
                action="link" if entry.kind == "link" else "create",
                target_ledger_id=payload.target_ledger_id,
                target_file_id=target.id,
                operation=financial,
            )
            response_json = response.model_dump(mode="json")
            canonical_json(response_json, CANDIDATE_BYTES)
            session.add(
                OcrConfirmation(
                    id=uuid4(),
                    ledger_id=ledger_id,
                    created_by=owner_id,
                    draft_id=draft.id,
                    intent_id=payload.intent_id,
                    source_file_id=source.id,
                    source_key=draft.source_key,
                    target_ledger_id=payload.target_ledger_id,
                    target_file_id=target.id,
                    operation_id=financial.id,
                    operation_version=financial.version,
                    draft_version=draft.version,
                    action=response.action,
                    hash_version=2,
                    request_hash=digest,
                    review=fields,
                    response=response_json,
                )
            )
            draft.fields, draft.status = fields, "confirmed"
            _touch(draft)
            session.flush()
            return response
