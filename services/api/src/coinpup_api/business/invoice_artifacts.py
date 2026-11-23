"""Prepare durable private bytes outside a transaction; never complete an issuance."""

from dataclasses import dataclass
from functools import partial

import anyio

from coinpup_api.business.invoice_pdf import MAX_INVOICE_PDF_BYTES
from coinpup_api.business.invoice_render_process import RenderedInvoicePdf, render_invoice_pair
from coinpup_api.files.storage import FileStore, FileStoreError
from coinpup_api.ledger.errors import MoneyError


@dataclass(frozen=True, slots=True)
class PreparedInvoiceBlob:
    language: str
    blob_key: str
    sha256: str
    byte_size: int
    media_type: str = "application/pdf"


@dataclass(frozen=True, slots=True)
class PreparedInvoiceBlobs:
    snapshot_sha256: str
    artifacts: tuple[PreparedInvoiceBlob, PreparedInvoiceBlob]


def _error(code="invoice_artifact_storage_unavailable"):
    return MoneyError(code, "Both private Invoice originals could not be prepared safely.")


def _checked_pair(pair):
    if type(pair) is not tuple or len(pair) != 2:
        raise _error("invoice_render_integrity")
    for language, item in zip(("zh", "en"), pair, strict=True):
        if (
            not isinstance(item, RenderedInvoicePdf)
            or item.language != language
            or type(item.content) is not bytes
            or not 1 <= len(item.content) <= MAX_INVOICE_PDF_BYTES
            or not item.content.startswith(b"%PDF-")
            or not item.content.endswith(b"%%EOF\n")
        ):
            raise _error("invoice_render_integrity")
    return pair


async def _chunks(content):
    for offset in range(0, len(content), 64 * 1024):
        yield content[offset : offset + 64 * 1024]


def _verify_published(store, blob):
    # FileStore verifies the complete bytes before returning this pinned open handle.
    with store.open_blob(blob.blob_key, blob.sha256, blob.byte_size):
        pass


async def prepare_invoice_blobs(
    content, *, expected_sha256, font_directory, store: FileStore, logo_content=None
):
    """Render the owned snapshot and durably prepare both originals, without DB writes.

    The caller authorizes immutable input and must hold no database transaction here.
    These are private blob identities, not file metadata, a success receipt or evidence
    that an issuance committed. Completion must recheck scope/snapshot under its locks.
    Published blobs survive every error/cancellation as potential referenced originals;
    only staging tokens are discarded, using the existing FileStore cleanup contract.
    """
    stages = []
    try:
        render = partial(
            render_invoice_pair,
            content,
            expected_sha256=expected_sha256,
            font_directory=font_directory,
            logo_content=logo_content,
        )
        # Do not abandon the thread: rendering owns and must reap its child process.
        pair = _checked_pair(await anyio.to_thread.run_sync(render))
        for item in pair:
            staged = await store.stage(_chunks(item.content), len(item.content))
            stages.append(staged)
            if (staged.sha256, staged.byte_size, staged.media_type) != (
                item.sha256,
                len(item.content),
                "application/pdf",
            ):
                raise _error("invoice_render_integrity")
        blobs = []
        for item, staged in zip(pair, stages, strict=True):
            key = await anyio.to_thread.run_sync(store.publish, staged)
            blobs.append(PreparedInvoiceBlob(item.language, key, item.sha256, len(item.content)))
        for blob in blobs:
            await anyio.to_thread.run_sync(_verify_published, store, blob)
        return PreparedInvoiceBlobs(expected_sha256, tuple(blobs))
    except (FileStoreError, OSError):
        raise _error() from None
    finally:
        cleanup_failed = False
        with anyio.CancelScope(shield=True):
            for staged in stages:
                try:
                    await anyio.to_thread.run_sync(store.discard, staged)
                except (FileStoreError, OSError):
                    cleanup_failed = True
        if cleanup_failed:
            raise _error() from None
