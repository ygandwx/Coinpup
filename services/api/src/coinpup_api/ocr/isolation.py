"""Linux process resource limits, not a network or filesystem security sandbox."""

import json
import math
import os
import selectors
import signal
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

_BOOTSTRAP_PATH = Path(__file__).with_name("_isolation_child.py")
_REQUEST_BYTES = 1048576
_MESSAGES = {
    "processor_unavailable": "OCR processing is unavailable.",
    "processor_timeout": "OCR processing timed out.",
    "resource_limit": "OCR processing exceeded its resource limits.",
    "processing_failed": "OCR processing failed.",
    "processing_cleanup_failed": "OCR process cleanup could not be confirmed.",
    "processing_cancelled": "OCR processing was cancelled.",
}


class IsolationError(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(_MESSAGES[code])


@dataclass(frozen=True)
class ProcessBudget:
    wall_seconds: int
    cpu_seconds: int
    address_space_bytes: int
    file_bytes: int
    open_files: int
    output_bytes: int

    def __post_init__(self):
        if any(type(value) is not int or not 1 <= value < 2**63 for value in vars(self).values()):
            raise ValueError("Invalid isolation budget.")
        if (
            self.wall_seconds > 86400
            or self.cpu_seconds > 86400
            or self.address_space_bytes > 2**40
            or self.file_bytes > 2**34
            or not 8 <= self.open_files <= 65536
            or self.output_bytes > _REQUEST_BYTES
        ):
            raise ValueError("Invalid isolation budget.")


@dataclass(frozen=True)
class ProcessResult:
    output: bytes = field(repr=False)
    stderr_bytes: int
    elapsed_seconds: float


def _json_value(value):
    if type(value) is dict:
        if any(type(key) is not str for key in value):
            raise ValueError
        for item in value.values():
            _json_value(item)
    elif type(value) is list:
        for item in value:
            _json_value(item)
    elif value is not None and type(value) not in (str, bool, int):
        if type(value) is not float or not math.isfinite(value):
            raise ValueError


def _request_bytes(request):
    try:
        if type(request) is not dict:
            raise ValueError
        _json_value(request)
        encoded = json.dumps(request, ensure_ascii=False, allow_nan=False).encode("utf-8")
        if len(encoded) > _REQUEST_BYTES:
            raise ValueError
        return encoded
    except (ValueError, TypeError, UnicodeError, RecursionError, OverflowError):
        raise IsolationError("processing_failed") from None


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result


def _reject_constant(value):
    raise ValueError


def _environment(directory):
    # Never inherit secrets, Python import overrides, proxy variables or dynamic loader hooks.
    return {
        "PATH": "/usr/bin:/bin",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "TMPDIR": str(directory),
        "OMP_NUM_THREADS": "1",
        "OPENBLAS_NUM_THREADS": "1",
        "MKL_NUM_THREADS": "1",
    }


def _stop(process):
    failed = False
    for sent, grace in ((signal.SIGTERM, 0.2), (signal.SIGKILL, 1.0)):
        # Even after the leader exits, descendants may still own its process group and pipes.
        try:
            os.killpg(process.pid, sent)
        except ProcessLookupError:
            pass
        except OSError:
            failed = True
        try:
            process.wait(timeout=grace)
        except subprocess.TimeoutExpired:
            if sent == signal.SIGKILL:
                failed = True
        except OSError:
            failed = True
    if failed:
        raise IsolationError("processing_cleanup_failed") from None


def _heartbeat(callback):
    """Only the parent may renew a lease; errors must not expose database details."""
    if callback is None:
        return
    try:
        active = callback()
    except Exception:
        raise IsolationError("processing_cancelled") from None
    if active is not True:
        raise IsolationError("processing_cancelled") from None


def _collect(process, budget, deadline, stop, heartbeat=None):
    output = bytearray()
    stderr_bytes = total = 0
    with selectors.DefaultSelector() as selector:
        for stream in (process.stdout, process.stderr):
            os.set_blocking(stream.fileno(), False)
            selector.register(stream, selectors.EVENT_READ)
        while selector.get_map() or process.poll() is None:
            _heartbeat(heartbeat)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise IsolationError("processor_timeout") from None
            if process.poll() is not None:
                stop()
            for key, _ in selector.select(min(remaining, 0.05)):
                try:
                    chunk = os.read(key.fd, min(65536, budget.output_bytes - total + 1))
                except (BlockingIOError, InterruptedError):
                    continue
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                total += len(chunk)
                if total > budget.output_bytes:
                    raise IsolationError("resource_limit") from None
                if key.fileobj is process.stdout:
                    output.extend(chunk)
                else:
                    stderr_bytes += len(chunk)
    return bytes(output), stderr_bytes


def run_isolated(request: dict, budget: ProcessBudget, *, heartbeat=None) -> ProcessResult:
    encoded = _request_bytes(request)
    if sys.platform != "linux":
        raise IsolationError("processor_unavailable") from None
    started = time.monotonic()
    process = temporary = None
    stopped = False

    def stop():
        nonlocal stopped
        if process is not None and not stopped:
            _stop(process)
            stopped = True

    try:
        _heartbeat(heartbeat)
        temporary = tempfile.TemporaryDirectory(prefix="coinpup-ocr-")
        directory = Path(temporary.name)
        os.chmod(directory, 0o700)
        descriptor, filename = tempfile.mkstemp(dir=directory, suffix=".json")
        with os.fdopen(descriptor, "wb") as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(encoded)
        command = [
            str(Path(sys.executable).absolute()),
            "-I",
            "-S",
            str(_BOOTSTRAP_PATH.resolve()),
            filename,
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
        output, stderr_bytes = _collect(
            process, budget, started + budget.wall_seconds, stop, heartbeat
        )
        _heartbeat(heartbeat)
        if process.returncode != 0:
            code = "processing_failed"
            if process.returncode == 70:
                code = "processor_unavailable"
            elif process.returncode in (72, -signal.SIGXCPU, -signal.SIGXFSZ, -signal.SIGKILL):
                code = "resource_limit"
            raise IsolationError(code) from None
        try:
            result = json.loads(
                output.decode("utf-8"),
                object_pairs_hook=_unique_object,
                parse_constant=_reject_constant,
            )
            _json_value(result)
            if type(result) is not dict:
                raise ValueError
        except (ValueError, TypeError, UnicodeError, RecursionError):
            raise IsolationError("processing_failed") from None
    except (OSError, subprocess.SubprocessError, ValueError):
        raise IsolationError(
            "processor_unavailable" if process is None else "processing_failed"
        ) from None
    finally:
        try:
            stop()
        finally:
            close_failed = False
            try:
                if process is not None:
                    for stream in (process.stdout, process.stderr):
                        if stream is not None:
                            try:
                                stream.close()
                            except OSError:
                                close_failed = True
            finally:
                if temporary is not None:
                    try:
                        temporary.cleanup()
                    except OSError:
                        close_failed = True
                if close_failed:
                    raise IsolationError("processing_cleanup_failed") from None
    return ProcessResult(output, stderr_bytes, time.monotonic() - started)
