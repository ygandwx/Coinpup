"""Linux resource-bounded Invoice rendering; not a filesystem or network security sandbox."""

import hashlib
import hmac
import os
import re
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field, fields
from pathlib import Path

from coinpup_api.business.invoice_logo import MAX_LOGO_BYTES
from coinpup_api.business.invoice_pdf import MAX_INVOICE_PDF_BYTES
from coinpup_api.business.invoice_render_snapshot import MAX_RENDER_SNAPSHOT_BYTES
from coinpup_api.ledger.errors import MoneyError
from coinpup_api.ocr.isolation import IsolationError, _collect, _environment, _stop

_BOOTSTRAP_PATH = Path(__file__).with_name("_invoice_render_child.py")
MAGIC = b"CPDF1\x00"
HEADER_BYTES = len(MAGIC) + 32 + 8 + 64
CHILD_ERRORS = {
    70: "invoice_render_unavailable",
    71: "invoice_render_invalid",
    72: "invoice_render_limit",
    73: "invoice_render_integrity",
    74: "invoice_fonts_unavailable",
    75: "invoice_render_glyph",
    76: "invoice_render_logo_unavailable",
    77: "invoice_render_logo_invalid",
}


@dataclass(frozen=True, slots=True)
class InvoiceRenderBudget:
    wall_seconds: int = 45
    cpu_seconds: int = 30
    address_space_bytes: int = 1024**3
    file_bytes: int = MAX_INVOICE_PDF_BYTES
    open_files: int = 64
    output_bytes: int = 2 * MAX_INVOICE_PDF_BYTES + HEADER_BYTES

    def __post_init__(self):
        for item in fields(self):
            value = getattr(self, item.name)
            minimum = 8 if item.name == "open_files" else 1
            if type(value) is not int or not minimum <= value <= item.default:
                raise ValueError("Invalid Invoice rendering budget.")


DEFAULT_BUDGET = InvoiceRenderBudget()


@dataclass(frozen=True, slots=True)
class RenderedInvoicePdf:
    language: str
    content: bytes = field(repr=False)

    @property
    def sha256(self):
        return hashlib.sha256(self.content).hexdigest()


def _error(code="invoice_render_invalid"):
    return MoneyError(code, "The frozen Invoice rendering process did not complete safely.")


def _pair(output, digest):
    if len(output) < HEADER_BYTES or output[: len(MAGIC)] != MAGIC:
        raise _error()
    position = len(MAGIC)
    if not hmac.compare_digest(output[position : position + 32], bytes.fromhex(digest)):
        raise _error("invoice_render_integrity")
    position += 32
    lengths = [int.from_bytes(output[position + i : position + i + 4], "big") for i in (0, 4)]
    position += 8
    if any(not 1 <= size <= MAX_INVOICE_PDF_BYTES for size in lengths):
        raise _error("invoice_render_limit")
    if len(output) != HEADER_BYTES + sum(lengths):
        raise _error()
    hashes = [output[position + i : position + i + 32] for i in (0, 32)]
    position = HEADER_BYTES
    result = []
    for language, size, digest_bytes in zip(("zh", "en"), lengths, hashes, strict=True):
        content = output[position : position + size]
        if (
            not hmac.compare_digest(hashlib.sha256(content).digest(), digest_bytes)
            or not content.startswith(b"%PDF-")
            or not content.endswith(b"%%EOF\n")
        ):
            raise _error("invoice_render_integrity")
        result.append(RenderedInvoicePdf(language, content))
        position += size
    return tuple(result)


def render_invoice_pair(
    content, *, expected_sha256, font_directory, budget=DEFAULT_BUDGET, logo_content=None
):
    """Return both complete PDFs from authorized immutable bytes; never finalize issuance.

    The caller supplies its trusted local font directory and owned reservation digest, not
    user-selectable paths or executables. A digest alone does not authorize an issuance.
    Original immutable input remains the recovery source after every failure.
    """
    if sys.platform != "linux":
        raise _error("invoice_render_unavailable")
    if not isinstance(budget, InvoiceRenderBudget):
        raise _error()
    if logo_content is not None:
        if type(logo_content) is not bytes:
            raise _error("invoice_render_integrity")
        if not 1 <= len(logo_content) <= MAX_LOGO_BYTES:
            raise _error("invoice_render_limit")
    if (
        type(content) is not bytes
        or not 1 <= len(content) <= MAX_RENDER_SNAPSHOT_BYTES
        or type(expected_sha256) is not str
        or re.fullmatch(r"[a-f0-9]{64}", expected_sha256) is None
        or not hmac.compare_digest(hashlib.sha256(content).hexdigest(), expected_sha256)
    ):
        raise _error("invoice_render_integrity")
    process = temporary = None
    stopped = False
    started = time.monotonic()

    def stop():
        nonlocal stopped
        if process is not None and not stopped:
            _stop(process)
            stopped = True

    try:
        font_path = Path(font_directory).resolve(strict=True)
        if not font_path.is_dir():
            raise _error("invoice_fonts_unavailable")
        temporary = tempfile.TemporaryDirectory(prefix="coinpup-invoice-")
        directory = Path(temporary.name)
        os.chmod(directory, 0o700)
        with (directory / "snapshot.json").open("xb") as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(content)
        if logo_content is not None:
            with (directory / "logo.bin").open("xb") as stream:
                os.fchmod(stream.fileno(), 0o600)
                stream.write(logo_content)
        command = [
            str(Path(sys.executable).absolute()),
            "-I",
            "-S",
            str(_BOOTSTRAP_PATH.resolve()),
            expected_sha256,
            str(font_path),
            str(budget.address_space_bytes),
            str(budget.cpu_seconds),
            str(budget.file_bytes),
            str(budget.open_files),
        ]
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=directory,
            env=_environment(directory),
            shell=False,
            close_fds=True,
            start_new_session=True,
            umask=0o077,
        )
        output, _ = _collect(process, budget, started + budget.wall_seconds, stop)
        if process.returncode:
            import signal

            code = CHILD_ERRORS.get(process.returncode, "invoice_render_invalid")
            if process.returncode in (-signal.SIGXCPU, -signal.SIGXFSZ, -signal.SIGKILL):
                code = "invoice_render_limit"
            raise _error(code)
        return _pair(output, expected_sha256)
    except IsolationError as error:
        code = {
            "processor_timeout": "invoice_render_timeout",
            "resource_limit": "invoice_render_limit",
            "processing_cleanup_failed": "invoice_render_cleanup_failed",
        }.get(error.code, "invoice_render_invalid")
        raise _error(code) from None
    except (OSError, subprocess.SubprocessError, TypeError, ValueError) as error:
        if isinstance(error, MoneyError):
            raise
        raise _error(
            "invoice_render_unavailable" if process is None else "invoice_render_invalid"
        ) from None
    finally:
        cleanup_failed = False
        try:
            stop()
        except IsolationError:
            cleanup_failed = True
        if process is not None:
            for stream in (process.stdout, process.stderr):
                try:
                    stream.close()
                except OSError:
                    cleanup_failed = True
        if temporary is not None:
            try:
                temporary.cleanup()
            except OSError:
                cleanup_failed = True
        if cleanup_failed:
            raise _error("invoice_render_cleanup_failed") from None
