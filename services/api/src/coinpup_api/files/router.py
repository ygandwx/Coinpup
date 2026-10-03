"""Authenticated raw streaming uploads and private attachment downloads."""

import logging
import re
from typing import Annotated
from urllib.parse import quote
from uuid import UUID

import anyio
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import StreamingResponse
from sqlalchemy.exc import SQLAlchemyError
from starlette.background import BackgroundTask
from starlette.requests import ClientDisconnect

from coinpup_api.access import BrowserAccess
from coinpup_api.auth import Identity
from coinpup_api.files.schemas import (
    MEDIA_TYPES,
    FileConfiguration,
    FileResponse,
    FileUpdate,
    LinkResponse,
    LinkUpdate,
    UploadCompletion,
    UploadCreate,
    UploadResponse,
)
from coinpup_api.files.service import DocumentService
from coinpup_api.files.storage import FileStore, FileStoreError
from coinpup_api.ledger.service import LedgerError

logger = logging.getLogger("coinpup.documents")


def _failure(code, status, message):
    return HTTPException(status, {"code": code, "message": message})


def _execute(operation, *args, **kwargs):
    try:
        return operation(*args, **kwargs)
    except LedgerError as error:
        raise _failure(error.code, error.status, str(error)) from None
    except FileStoreError as error:
        raise _failure(error.code, error.status_code, "The file could not be processed.") from None
    except (SQLAlchemyError, OSError):
        logger.warning("Private document storage unavailable")
        raise _failure("file_unavailable", 503, "File storage is unavailable.") from None


def create_document_router(settings, engine, store=None) -> APIRouter:
    router = APIRouter(prefix="/api/v1", tags=["private documents"])
    service = DocumentService(engine, settings.max_upload_bytes)
    store = (
        store
        if store is not None
        else FileStore(
            settings.files_directory, settings.max_upload_bytes, settings.upload_timeout_seconds
        )
    )
    access = BrowserAccess(settings, engine)
    reader = Annotated[Identity, Depends(access.read)]
    writer = Annotated[Identity, Depends(access.write)]
    page_size = Annotated[int, Query(ge=1, le=200)]
    page_offset = Annotated[int, Query(ge=0, le=100000)]

    @router.get("/files/configuration", response_model=FileConfiguration)
    def configuration(identity: reader):
        return FileConfiguration(
            max_upload_bytes=settings.max_upload_bytes,
            upload_timeout_seconds=settings.upload_timeout_seconds,
            supported_media_types=list(MEDIA_TYPES),
        )

    @router.post("/ledgers/{ledger_id}/uploads", response_model=UploadResponse, status_code=201)
    def reserve(ledger_id: UUID, body: UploadCreate, identity: writer):
        return _execute(service.reserve_upload, identity.id, ledger_id, body)

    @router.get("/ledgers/{ledger_id}/uploads/{upload_id}", response_model=UploadResponse)
    def upload_status(ledger_id: UUID, upload_id: UUID, identity: reader):
        return _execute(service.get_upload, identity.id, ledger_id, upload_id)

    @router.put(
        "/ledgers/{ledger_id}/uploads/{upload_id}/content",
        response_model=UploadCompletion,
        openapi_extra={
            "requestBody": {
                "required": True,
                "content": {
                    "application/octet-stream": {"schema": {"type": "string", "format": "binary"}}
                },
            }
        },
    )
    async def upload_content(ledger_id: UUID, upload_id: UUID, request: Request, identity: writer):
        # Do not declare a Body/UploadFile parameter: request.stream() must remain incremental.
        if request.headers.get("content-encoding", "identity").lower() != "identity":
            raise _failure(
                "unsupported_content_encoding", 415, "Compressed uploads are not supported."
            )
        if request.headers.get("content-type", "").lower() != "application/octet-stream":
            raise _failure(
                "unsupported_content_type", 415, "Use application/octet-stream for uploads."
            )
        upload = await run_in_threadpool(
            _execute, service.get_upload, identity.id, ledger_id, upload_id, for_upload=True
        )
        # Declared length is an early rejection only; stage enforces each actual byte received.
        lengths = request.headers.getlist("content-length")
        if lengths:
            if len(lengths) != 1 or not re.fullmatch(r"[0-9]{1,20}", lengths[0]):
                raise _failure("invalid_content_length", 422, "Content length is invalid.")
            length = int(lengths[0])
            if length > settings.max_upload_bytes:
                raise _failure("file_too_large", 413, "The file exceeds the upload limit.")
            if length != upload.declared_size:
                raise _failure(
                    "upload_size_mismatch", 422, "Content length does not match the reservation."
                )
        if upload.declared_size > settings.max_upload_bytes:
            raise _failure("file_too_large", 413, "The file exceeds the upload limit.")
        staged = None
        try:
            try:
                staged = await store.stage(request.stream(), upload.declared_size)
            except FileStoreError as error:
                raise _failure(
                    error.code, error.status_code, "The file could not be processed."
                ) from None
            except OSError:
                raise _failure("file_unavailable", 503, "File storage is unavailable.") from None
            except ClientDisconnect:
                raise _failure("upload_interrupted", 400, "The upload was interrupted.") from None
            # The session may have expired while the client was sending the body.
            current = await run_in_threadpool(
                access.write, request, request.headers.get("x-csrf-token")
            )
            if current.id != identity.id:
                raise _failure("authentication_required", 401, "Authentication is required.")
            publish = await run_in_threadpool(
                _execute, service.needs_publish, identity.id, ledger_id, upload_id, staged
            )
            if publish:
                blob_key = await run_in_threadpool(_execute, store.publish, staged)
            else:
                existing = await run_in_threadpool(
                    _execute, service.get_existing_blob, identity.id, ledger_id, upload_id, staged
                )

                def verify_existing():
                    with store.open_blob(*existing):
                        pass

                await run_in_threadpool(_execute, verify_existing)
                blob_key = None
            return await run_in_threadpool(
                _execute,
                service.finalize_upload,
                identity.id,
                ledger_id,
                upload_id,
                staged,
                blob_key,
            )
        finally:
            if staged is not None:
                with anyio.CancelScope(shield=True):
                    try:
                        await run_in_threadpool(store.discard, staged)
                    except (OSError, FileStoreError):
                        # Cleanup failure must not mask a committed receipt.
                        logger.warning("Private document staging cleanup incomplete")

    @router.get("/ledgers/{ledger_id}/files", response_model=list[FileResponse])
    def list_files(
        ledger_id: UUID,
        identity: reader,
        include_archived: bool = False,
        limit: page_size = 100,
        offset: page_offset = 0,
    ):
        return _execute(service.list_files, identity.id, ledger_id, include_archived, limit, offset)

    @router.get("/ledgers/{ledger_id}/files/{file_id}", response_model=FileResponse)
    def file_metadata(ledger_id: UUID, file_id: UUID, identity: reader):
        return _execute(service.get_file, identity.id, ledger_id, file_id)

    @router.patch("/ledgers/{ledger_id}/files/{file_id}", response_model=FileResponse)
    def file_update(ledger_id: UUID, file_id: UUID, body: FileUpdate, identity: writer):
        return _execute(service.update_file, identity.id, ledger_id, file_id, body)

    @router.get(
        "/ledgers/{ledger_id}/files/{file_id}/content",
        response_class=StreamingResponse,
        responses={200: {"content": {media: {} for media in MEDIA_TYPES}}},
    )
    def download(ledger_id: UUID, file_id: UUID, identity: reader):
        file, blob_key = _execute(service.get_download, identity.id, ledger_id, file_id)
        handle = _execute(store.open_blob, blob_key, file.sha256, file.byte_size)

        def chunks():
            try:
                while chunk := handle.read(64 * 1024):
                    yield chunk
            finally:
                handle.close()

        extension = {
            "application/pdf": "pdf",
            "image/jpeg": "jpg",
            "image/png": "png",
            "image/webp": "webp",
        }[file.detected_media_type]
        disposition = (
            f'attachment; filename="file-{file.id}.{extension}"; '
            f"filename*=UTF-8''{quote(file.original_filename, safe='')}"
        )
        return StreamingResponse(
            chunks(),
            media_type=file.detected_media_type,
            background=BackgroundTask(handle.close),
            headers={
                "Content-Length": str(file.byte_size),
                "Content-Disposition": disposition,
                "Cache-Control": "private, no-store",
                "X-Content-Type-Options": "nosniff",
                "Content-Security-Policy": "sandbox; default-src 'none'",
            },
        )

    @router.get(
        "/ledgers/{ledger_id}/operations/{operation_id}/files", response_model=list[LinkResponse]
    )
    def links(
        ledger_id: UUID,
        operation_id: UUID,
        identity: reader,
        include_archived: bool = True,
        limit: page_size = 100,
        offset: page_offset = 0,
    ):
        return _execute(
            service.list_operation_files,
            identity.id,
            ledger_id,
            operation_id,
            include_archived,
            limit,
            offset,
        )

    @router.put(
        "/ledgers/{ledger_id}/operations/{operation_id}/files/{file_id}",
        response_model=LinkResponse,
    )
    def create_link(ledger_id: UUID, operation_id: UUID, file_id: UUID, identity: writer):
        return _execute(service.link_file, identity.id, ledger_id, operation_id, file_id)

    @router.patch(
        "/ledgers/{ledger_id}/operations/{operation_id}/files/{file_id}",
        response_model=LinkResponse,
    )
    def change_link(
        ledger_id: UUID, operation_id: UUID, file_id: UUID, body: LinkUpdate, identity: writer
    ):
        return _execute(service.update_link, identity.id, ledger_id, operation_id, file_id, body)

    return router
