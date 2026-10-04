"""Short owner-scoped metadata transactions; blob IO always occurs outside them."""

import hashlib
import json
import re
from uuid import uuid4

from sqlalchemy import func, select

from coinpup_api.files.models import FileUpload, OperationFileLink, StoredFile
from coinpup_api.files.schemas import (
    MEDIA_TYPES,
    FileResponse,
    FileUpdate,
    LinkResponse,
    LinkUpdate,
    UploadCompletion,
    UploadCreate,
    UploadResponse,
)
from coinpup_api.ledger.models import Entity, FinancialOperation, Ledger
from coinpup_api.ledger.service import (
    LedgerError,
    LedgerService,
    _not_found,
    _page,
    _touch,
    _version,
)


def _active(entity):
    if entity.archived:
        raise LedgerError("entity_archived", 409, "Restore the entity before changing its files.")


def _content_conflict():
    return LedgerError("upload_content_conflict", 409, "The upload content does not match.")


class DocumentService(LedgerService):
    def __init__(self, engine, max_upload_bytes=50 * 1024 * 1024):
        super().__init__(engine)
        self.max_upload_bytes = max_upload_bytes

    def _locked_scope(self, session, owner_id, ledger_id):
        ledger = session.scalar(
            select(Ledger)
            .where(Ledger.id == ledger_id, Ledger.owner_id == owner_id)
            .with_for_update()
        )
        if ledger is None:
            raise _not_found()
        entity = session.scalar(
            select(Entity)
            .where(Entity.id == ledger.entity_id, Entity.owner_id == owner_id)
            .with_for_update()
        )
        if entity is None:
            raise _not_found()
        return entity

    @staticmethod
    def _record(session, model, ledger_id, record_id):
        record = session.scalar(
            select(model).where(model.id == record_id, model.ledger_id == ledger_id)
        )
        if record is None:
            raise _not_found()
        return record

    @staticmethod
    def _canonical(session, ledger_id, staged):
        return session.scalar(
            select(StoredFile).where(
                StoredFile.ledger_id == ledger_id,
                StoredFile.sha256 == staged.sha256,
                StoredFile.byte_size == staged.byte_size,
            )
        )

    def _verify_staged(self, upload, staged):
        if (
            type(staged.byte_size) is not int
            or staged.byte_size < 1
            or staged.byte_size != upload.declared_size
            or not re.fullmatch(r"[0-9a-f]{64}", staged.sha256)
            or staged.media_type not in MEDIA_TYPES
        ):
            raise _content_conflict()
        if staged.byte_size > self.max_upload_bytes:
            raise LedgerError("file_too_large", 413, "The file exceeds the upload limit.")

    @staticmethod
    def _same_content(file, staged):
        if (file.sha256, file.byte_size, file.detected_media_type) != (
            staged.sha256,
            staged.byte_size,
            staged.media_type,
        ):
            raise _content_conflict()

    def reserve_upload(self, owner_id, ledger_id, payload: UploadCreate) -> UploadResponse:
        manifest = hashlib.sha256(
            json.dumps(
                payload.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
            ).encode()
        ).hexdigest()
        with self._transaction(owner_id, write=True) as session:
            entity = self._locked_scope(session, owner_id, ledger_id)
            existing = session.scalar(
                select(FileUpload).where(
                    FileUpload.id == payload.id, FileUpload.ledger_id == ledger_id
                )
            )
            if existing is not None:
                if existing.manifest_hash != manifest:
                    raise LedgerError(
                        "upload_manifest_conflict", 409, "The upload request changed."
                    )
                return UploadResponse.model_validate(existing)
            _active(entity)
            if payload.declared_size > self.max_upload_bytes:
                raise LedgerError("file_too_large", 413, "The file exceeds the upload limit.")
            if payload.operation_id is not None:
                self._record(session, FinancialOperation, ledger_id, payload.operation_id)
            # A client UUID belonging to another ledger is indistinguishable from missing data.
            if session.get(FileUpload, payload.id) is not None:
                raise _not_found()
            upload = FileUpload(
                **payload.model_dump(),
                ledger_id=ledger_id,
                created_by=owner_id,
                manifest_hash=manifest,
            )
            session.add(upload)
            session.flush()
            return UploadResponse.model_validate(upload)

    def get_upload(self, owner_id, ledger_id, upload_id, *, for_upload=False) -> UploadResponse:
        with self._transaction(owner_id) as session:
            _, entity = self._ledger(session, owner_id, ledger_id)
            upload = self._record(session, FileUpload, ledger_id, upload_id)
            if for_upload and upload.state != "ready":
                _active(entity)
            return UploadResponse.model_validate(upload)

    def needs_publish(self, owner_id, ledger_id, upload_id, staged) -> bool:
        """A hint only: finalize repeats these checks while holding the ledger lock."""
        with self._transaction(owner_id) as session:
            _, entity = self._ledger(session, owner_id, ledger_id)
            upload = self._record(session, FileUpload, ledger_id, upload_id)
            self._verify_staged(upload, staged)
            if upload.state == "ready":
                self._same_content(
                    self._record(session, StoredFile, ledger_id, upload.file_id), staged
                )
                return False
            _active(entity)
            file = self._canonical(session, ledger_id, staged)
            if file is not None:
                self._same_content(file, staged)
            return file is None

    def get_existing_blob(self, owner_id, ledger_id, upload_id, staged):
        """Return immutable blob identity for verification outside the DB transaction."""
        with self._transaction(owner_id) as session:
            self._ledger(session, owner_id, ledger_id)
            upload = self._record(session, FileUpload, ledger_id, upload_id)
            self._verify_staged(upload, staged)
            file = (
                self._record(session, StoredFile, ledger_id, upload.file_id)
                if upload.state == "ready"
                else self._canonical(session, ledger_id, staged)
            )
            if file is None:
                raise LedgerError(
                    "file_unavailable", 503, "The file is not ready; retry the upload."
                )
            self._same_content(file, staged)
            return file.blob_key, file.sha256, file.byte_size

    def _link(self, session, owner_id, ledger_id, operation_id, file):
        self._record(session, FinancialOperation, ledger_id, operation_id)
        link = session.scalar(
            select(OperationFileLink).where(
                OperationFileLink.ledger_id == ledger_id,
                OperationFileLink.operation_id == operation_id,
                OperationFileLink.file_id == file.id,
            )
        )
        if link is not None:
            return link
        if file.archived:
            raise LedgerError(
                "file_archived", 409, "Restore the file before adding an association."
            )
        link = OperationFileLink(
            id=uuid4(),
            ledger_id=ledger_id,
            operation_id=operation_id,
            file_id=file.id,
            created_by=owner_id,
        )
        session.add(link)
        session.flush()
        return link

    def finalize_upload(
        self, owner_id, ledger_id, upload_id, staged, blob_key=None
    ) -> UploadCompletion:
        """Commit metadata only after the caller has durably published a complete blob."""
        with self._transaction(owner_id, write=True) as session:
            entity = self._locked_scope(session, owner_id, ledger_id)
            upload = self._record(session, FileUpload, ledger_id, upload_id)
            self._verify_staged(upload, staged)
            if upload.state == "ready":
                self._same_content(
                    self._record(session, StoredFile, ledger_id, upload.file_id), staged
                )
                return UploadCompletion.model_validate(upload.response)
            _active(entity)
            file = self._canonical(session, ledger_id, staged)
            duplicate = file is not None
            if file is None:
                if not isinstance(blob_key, str) or not re.fullmatch(r"[0-9a-f]{32}", blob_key):
                    raise LedgerError(
                        "file_unavailable", 503, "The file is not ready; retry the upload."
                    )
                file = StoredFile(
                    id=uuid4(),
                    ledger_id=ledger_id,
                    created_by=owner_id,
                    blob_key=blob_key,
                    sha256=staged.sha256,
                    byte_size=staged.byte_size,
                    detected_media_type=staged.media_type,
                    original_filename=upload.original_filename,
                    title=upload.original_filename,
                )
                session.add(file)
                session.flush()
            else:
                self._same_content(file, staged)
            link = (
                self._link(session, owner_id, ledger_id, upload.operation_id, file)
                if upload.operation_id is not None
                else None
            )
            result = UploadCompletion(
                upload_id=upload.id,
                file_id=file.id,
                link_id=link.id if link else None,
                duplicate=duplicate,
                sha256=file.sha256,
                byte_size=file.byte_size,
                media_type=file.detected_media_type,
            )
            upload.state = "ready"
            upload.file_id = file.id
            upload.response = result.model_dump(mode="json")
            upload.completed_at = func.clock_timestamp()
            session.flush()
            return result

    def get_file(self, owner_id, ledger_id, file_id) -> FileResponse:
        return self.get_download(owner_id, ledger_id, file_id)[0]

    def get_download(self, owner_id, ledger_id, file_id):
        with self._transaction(owner_id) as session:
            self._ledger(session, owner_id, ledger_id)
            file = self._record(session, StoredFile, ledger_id, file_id)
            return FileResponse.model_validate(file), file.blob_key

    def list_files(self, owner_id, ledger_id, include_archived=False, limit=100, offset=0):
        _page(limit, offset)
        with self._transaction(owner_id) as session:
            self._ledger(session, owner_id, ledger_id)
            query = select(StoredFile).where(StoredFile.ledger_id == ledger_id)
            if not include_archived:
                query = query.where(StoredFile.archived.is_(False))
            return [
                FileResponse.model_validate(file)
                for file in session.scalars(
                    query.order_by(StoredFile.id).limit(limit).offset(offset)
                )
            ]

    def update_file(self, owner_id, ledger_id, file_id, payload: FileUpdate) -> FileResponse:
        with self._transaction(owner_id, write=True) as session:
            _active(self._locked_scope(session, owner_id, ledger_id))
            file = self._record(session, StoredFile, ledger_id, file_id)
            _version(file, payload.expected_version)
            for key, value in payload.model_dump(exclude_unset=True).items():
                if key != "expected_version":
                    setattr(file, key, value)
            _touch(file)
            session.flush()
            return FileResponse.model_validate(file)

    def link_file(self, owner_id, ledger_id, operation_id, file_id) -> LinkResponse:
        with self._transaction(owner_id, write=True) as session:
            _active(self._locked_scope(session, owner_id, ledger_id))
            file = self._record(session, StoredFile, ledger_id, file_id)
            return LinkResponse.model_validate(
                self._link(session, owner_id, ledger_id, operation_id, file)
            )

    def list_operation_files(
        self, owner_id, ledger_id, operation_id, include_archived=True, limit=100, offset=0
    ):
        _page(limit, offset)
        with self._transaction(owner_id) as session:
            self._ledger(session, owner_id, ledger_id)
            self._record(session, FinancialOperation, ledger_id, operation_id)
            query = select(OperationFileLink).where(
                OperationFileLink.ledger_id == ledger_id,
                OperationFileLink.operation_id == operation_id,
            )
            if not include_archived:
                query = query.where(OperationFileLink.archived.is_(False))
            return [
                LinkResponse.model_validate(link)
                for link in session.scalars(
                    query.order_by(OperationFileLink.id).limit(limit).offset(offset)
                )
            ]

    def update_link(
        self, owner_id, ledger_id, operation_id, file_id, payload: LinkUpdate
    ) -> LinkResponse:
        with self._transaction(owner_id, write=True) as session:
            _active(self._locked_scope(session, owner_id, ledger_id))
            link = session.scalar(
                select(OperationFileLink).where(
                    OperationFileLink.ledger_id == ledger_id,
                    OperationFileLink.operation_id == operation_id,
                    OperationFileLink.file_id == file_id,
                )
            )
            if link is None:
                raise _not_found()
            _version(link, payload.expected_version)
            if not payload.archived:
                file = self._record(session, StoredFile, ledger_id, file_id)
                if file.archived:
                    raise LedgerError(
                        "file_archived", 409, "Restore the file before restoring an association."
                    )
            link.archived = payload.archived
            _touch(link)
            session.flush()
            return LinkResponse.model_validate(link)
