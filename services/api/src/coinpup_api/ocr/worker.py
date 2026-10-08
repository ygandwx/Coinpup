"""Single-job Linux worker; all file access and OCR happen outside queue transactions."""

import argparse
import hashlib
import json
import os
import sys
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from coinpup_api.config import Settings
from coinpup_api.database import Database
from coinpup_api.files.storage import FileStore, FileStoreError
from coinpup_api.ledger.service import LedgerError
from coinpup_api.models import Administrator

from .candidates import completion_from_result
from .contracts import Lease, prepare_configuration
from .isolation import IsolationError, ProcessBudget, run_isolated
from .pdf_probe import MAX_SOURCE_BYTES
from .queue import OcrQueueService
from .runtime import validate_processing


class _Pulse:
    def __init__(self, queue, lease, interval, clock):
        self.queue, self.lease, self.interval, self.clock = queue, lease, interval, clock
        self.next_at, self.cancelled = 0, False

    def __call__(self, *, force=False):
        if self.cancelled:
            return False
        if not force and self.clock() < self.next_at:
            return True
        try:
            renewed = self.queue.renew(self.lease)
            if not isinstance(renewed, Lease):
                self.cancelled = True
                return False
            self.lease = renewed
            self.next_at = self.clock() + self.interval
            return True
        except Exception:
            # An uncertain renewal must not be followed by completion or failure writes.
            self.cancelled = True
            return False


def _active(pulse):
    if not pulse(force=True):
        raise IsolationError("processing_cancelled")


@contextmanager
def _source(store, lease, pulse):
    suffix = {
        "application/pdf": "pdf",
        "image/jpeg": "jpg",
        "image/png": "png",
        "image/webp": "webp",
    }.get(lease.media_type)
    if suffix is None or not 1 <= lease.byte_size <= MAX_SOURCE_BYTES:
        raise FileStoreError("file_integrity_error")
    temporary = tempfile.TemporaryDirectory(prefix="coinpup-ocr-source-")
    try:
        directory = Path(temporary.name)
        os.chmod(directory, 0o700)
        path = directory / f"{uuid4().hex}.{suffix}"
        count, digest = 0, hashlib.sha256()
        _active(pulse)
        with store.open_blob(lease.blob_key, lease.sha256, lease.byte_size) as source:
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "wb") as target:
                while chunk := source.read(1024 * 1024):
                    if not pulse():
                        raise IsolationError("processing_cancelled")
                    count += len(chunk)
                    if count > lease.byte_size:
                        raise FileStoreError("file_integrity_error")
                    digest.update(chunk)
                    target.write(chunk)
                target.flush()
                os.fsync(target.fileno())
        if count != lease.byte_size or digest.hexdigest() != lease.sha256:
            raise FileStoreError("file_integrity_error")
        _active(pulse)
        yield {"path": str(path), "sha256": lease.sha256, "byte_size": count}
    finally:
        try:
            temporary.cleanup()
        except OSError:
            raise IsolationError("processing_cleanup_failed") from None


def _failure(queue, pulse, code, *, retryable=False):
    if pulse.cancelled:
        return "cancelled"
    try:
        return queue.fail(pulse.lease, code, retryable=retryable).state
    except LedgerError as error:
        if error.code == "ocr_lease_lost":
            return "cancelled"
        raise


def run_once(queue, store, owner_id, *, isolate=None, clock=time.monotonic):
    """A fake isolate may be injected by contract tests; production requires Linux."""
    if isolate is None:
        if sys.platform != "linux":
            raise IsolationError("processor_unavailable")
        isolate = run_isolated
    lease = queue.claim(owner_id)
    if lease is None:
        return "idle"
    try:
        config, _ = prepare_configuration(json.loads(lease.configuration_json))
        validate_processing(config["processing"])
    except (ValueError, TypeError, KeyError, OSError, LedgerError):
        return queue.fail(lease, "configuration_invalid").state
    pulse = _Pulse(queue, lease, config["lease_seconds"] / 3, clock)
    failure = None
    try:
        with _source(store, lease, pulse) as source:
            output = isolate(
                {
                    "version": 1,
                    "action": "recognize_document",
                    "source": source,
                    "media_type": lease.media_type,
                    "processing": config["processing"],
                },
                ProcessBudget(**config["processing"]["process"]),
                heartbeat=pulse,
            )
        # Source and child temporary files are gone before attempting any result write.
        _active(pulse)
        result = json.loads(output.output)
        if type(result) is not dict:
            raise ValueError
        if result.get("status") == "failed":
            reason = result.get("reason")
            if reason in {"engine_unavailable", "engine_initialization_failed"}:
                failure = ("processor_unavailable", True)
            else:
                failure = (
                    "resource_limit" if reason == "engine_limit" else "invalid_document",
                    False,
                )
        else:
            completion = completion_from_result(result)
    except IsolationError as error:
        if error.code == "processing_cleanup_failed":
            raise  # Do not claim another task after unconfirmed process/file cleanup.
        if error.code == "processing_cancelled":
            return "cancelled"
        return _failure(
            queue,
            pulse,
            error.code,
            retryable=error.code in {"processor_unavailable", "processor_timeout"},
        )
    except FileStoreError:
        return _failure(queue, pulse, "source_unavailable")
    except (OSError, ValueError, TypeError, KeyError, LedgerError):
        return _failure(queue, pulse, "processing_failed")
    # Keep uncertain commit errors outside the computation handler: never follow with fail().
    if failure is not None:
        return _failure(queue, pulse, failure[0], retryable=failure[1])
    try:
        return queue.finish(pulse.lease, completion).state
    except LedgerError as error:
        if error.code == "ocr_lease_lost":
            return "cancelled"
        raise


def main(argv=None):
    parser = argparse.ArgumentParser(description="Run Coinpup's private, single-job OCR worker")
    parser.add_argument("--once", action="store_true", help="Attempt one claim, then exit")
    args = parser.parse_args(argv)
    if sys.platform != "linux":
        print("OCR worker requires Linux isolation.", file=sys.stderr)
        return 1
    database = None
    try:
        settings = Settings()
        database = Database(settings)
        with database.engine.connect() as connection:
            owner = connection.scalar(select(Administrator.id))
        if owner is None:
            print("Initialize the administrator before starting OCR.", file=sys.stderr)
            return 1
        store = FileStore(
            settings.files_directory, settings.max_upload_bytes, settings.upload_timeout_seconds
        )
        queue = OcrQueueService(database.engine)
        while True:
            state = run_once(queue, store, owner)
            if state != "idle":
                print(f"OCR task state: {state}.", flush=True)
            if args.once:
                return 0
            time.sleep(2)
    except KeyboardInterrupt:
        return 0
    except (SQLAlchemyError, LedgerError, IsolationError, OSError, ValueError):
        print(
            "OCR worker stopped; check service availability and private storage.", file=sys.stderr
        )
        return 1
    finally:
        if database is not None:
            database.close()


if __name__ == "__main__":
    raise SystemExit(main())
