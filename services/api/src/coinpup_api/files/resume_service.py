"""Atomic part admission, restartable progress, and snapshot-based private staging."""

import hashlib
from dataclasses import dataclass

from sqlalchemy import func, select

from coinpup_api.files.models import FileUpload, StoredFile
from coinpup_api.files.resume_guard import (
    check_resume,
    require_active_device,
    require_not_abandoned,
)
from coinpup_api.files.resume_models import CHUNK_BYTES, UploadChunk, UploadResume
from coinpup_api.files.resume_schemas import UploadResumeStatus
from coinpup_api.files.schemas import UploadCompletion
from coinpup_api.files.service import DocumentService, _active, _content_conflict
from coinpup_api.ledger.service import LedgerError, _not_found


@dataclass(frozen=True)
class CompletedUpload:
    response: UploadCompletion
    blob_key: str


class ResumeAlreadyComplete(Exception):
    def __init__(self, completed):
        self.completed = completed


def _offset(session, upload_id):
    return session.scalar(
        select(func.coalesce(func.max(UploadChunk.byte_offset + UploadChunk.byte_size), 0)).where(
            UploadChunk.upload_id == upload_id
        )
    )


def _status(upload, manifest, offset):
    return UploadResumeStatus(
        upload_id=upload.id,
        ledger_id=upload.ledger_id,
        device_id=manifest.device_id,
        sha256=manifest.sha256,
        declared_size=upload.declared_size,
        next_offset=upload.declared_size if upload.state == "ready" else offset,
        max_chunk_bytes=CHUNK_BYTES,
        state="abandoned" if manifest.abandoned_at is not None else upload.state,
    )


class ResumableUploads(DocumentService):
    def status(self, owner, ledger, upload_id, device):
        with self._transaction(owner, read_only=True) as session:
            self._ledger(session, owner, ledger)
            upload = self._record(session, FileUpload, ledger, upload_id)
            manifest = check_resume(session, upload, device, existing=True)
            return _status(upload, manifest, _offset(session, upload_id))

    def initialize(self, owner, ledger, upload_id, device, digest):
        with self._transaction(owner, write=True, read_committed=True) as session:
            entity = self._locked_scope(session, owner, ledger)
            upload = self._record(session, FileUpload, ledger, upload_id)
            existing = check_resume(session, upload, device, digest=digest)
            if existing is not None:
                return _status(upload, existing, _offset(session, upload_id))
            require_active_device(session, owner, device)
            if upload.state == "ready":
                raise LedgerError("upload_complete", 409, "The upload is already complete.")
            if upload.declared_size > self.max_upload_bytes:
                raise LedgerError("file_too_large", 413, "The file exceeds the upload limit.")
            _active(entity)
            manifest = UploadResume(
                upload_id=upload_id,
                ledger_id=ledger,
                owner_id=owner,
                device_id=device,
                sha256=digest,
            )
            session.add(manifest)
            session.flush()
            return _status(upload, manifest, 0)

    def abandon(self, owner, ledger, upload_id):
        with self._transaction(owner, write=True, read_committed=True) as session:
            self._locked_scope(session, owner, ledger)
            upload = self._record(session, FileUpload, ledger, upload_id)
            if upload.state == "ready":
                raise LedgerError("upload_complete", 409, "The upload is already complete.")
            manifest = session.get(UploadResume, upload_id)
            if manifest is None:
                raise _not_found()
            if manifest.abandoned_at is None:
                manifest.abandoned_at = session.scalar(select(func.clock_timestamp()))
                session.flush()
            return _status(upload, manifest, _offset(session, upload_id))

    def append(self, owner, ledger, upload_id, device, offset, body):
        if type(offset) is not int or offset < 0 or offset % CHUNK_BYTES:
            raise LedgerError("upload_chunk_offset", 409, "Use an aligned upload byte offset.")
        if not body or len(body) > CHUNK_BYTES:
            raise LedgerError("upload_chunk_size", 422, "The upload part size is invalid.")
        digest = hashlib.sha256(body).hexdigest()
        with self._transaction(owner, write=True, read_committed=True) as session:
            entity = self._locked_scope(session, owner, ledger)
            upload = self._record(session, FileUpload, ledger, upload_id)
            manifest = check_resume(session, upload, device, existing=True)
            next_offset = _offset(session, upload_id)
            existing = session.get(UploadChunk, (upload_id, offset))
            if existing is not None:
                if (existing.sha256, existing.byte_size) != (digest, len(body)) or (
                    existing.payload is not None and existing.payload != body
                ):
                    raise _content_conflict()
                return _status(upload, manifest, next_offset).model_copy(
                    update={"state": "pending", "next_offset": offset + existing.byte_size}
                )
            if upload.state == "ready":
                raise LedgerError("upload_complete", 409, "The upload is already complete.")
            require_not_abandoned(manifest)
            require_active_device(session, owner, device)
            _active(entity)
            if offset != next_offset:
                raise LedgerError(
                    "upload_chunk_offset", 409, "Resume at the committed byte offset."
                )
            if len(body) != min(CHUNK_BYTES, upload.declared_size - offset):
                raise LedgerError("upload_chunk_size", 422, "The upload part size is invalid.")
            staged = session.scalar(
                select(func.coalesce(func.sum(UploadChunk.byte_size), 0))
                .join(UploadResume, UploadResume.upload_id == UploadChunk.upload_id)
                .where(UploadResume.owner_id == owner, UploadChunk.payload.is_not(None))
            )
            if staged + len(body) > 4 * self.max_upload_bytes:
                raise LedgerError("upload_staging_limit", 413, "The pending upload quota is full.")
            session.add(
                UploadChunk(
                    upload_id=upload_id,
                    ledger_id=ledger,
                    byte_offset=offset,
                    byte_size=len(body),
                    sha256=digest,
                    payload=body,
                )
            )
            session.flush()
            return _status(upload, manifest, offset + len(body))

    def parts(self, owner, ledger, upload_id, device):
        """Yield bounded parts in an untorn read snapshot even through another finalization."""
        with self._transaction(owner, read_only=True) as session:
            _, entity = self._ledger(session, owner, ledger)
            upload = self._record(session, FileUpload, ledger, upload_id)
            require_not_abandoned(check_resume(session, upload, device, existing=True))
            if upload.state == "ready":
                file = self._record(session, StoredFile, ledger, upload.file_id)
                raise ResumeAlreadyComplete(
                    CompletedUpload(UploadCompletion.model_validate(upload.response), file.blob_key)
                )
            require_active_device(session, owner, device)
            _active(entity)
            if _offset(session, upload_id) != upload.declared_size:
                raise LedgerError("upload_incomplete", 409, "Upload all parts before completing.")
            parts = session.execute(
                select(UploadChunk.payload)
                .where(UploadChunk.upload_id == upload_id)
                .order_by(UploadChunk.byte_offset)
                .execution_options(yield_per=1)
            ).scalars()
            for body in parts:
                if body is None:
                    raise LedgerError("file_unavailable", 503, "The upload data is unavailable.")
                yield body
