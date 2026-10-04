"""Fictional stdlib probes; these actions perform no document recognition."""

import errno
import json
import os
import resource
import signal
import sys
from pathlib import Path

RESOURCES = {
    "cpu_seconds": resource.RLIMIT_CPU,
    "address_space_bytes": resource.RLIMIT_AS,
    "file_bytes": resource.RLIMIT_FSIZE,
    "open_files": resource.RLIMIT_NOFILE,
}
IMPORT_LIMITS = {name: resource.getrlimit(number) for name, number in RESOURCES.items()}
IMPORT_ENV = dict(os.environ)


def process(request, work_directory):
    mode = request["mode"]
    if mode == "inspect":
        return {
            "import_limits": IMPORT_LIMITS,
            "call_limits": {name: resource.getrlimit(number) for name, number in RESOURCES.items()},
            "environment": IMPORT_ENV,
            "working_directory": str(work_directory),
            "request": request,
        }
    if mode == "memory":
        try:
            bytearray(request["allocation_bytes"])
        except MemoryError:
            return {"blocked": True}
        return {"blocked": False}
    if mode == "file":
        signal.signal(signal.SIGXFSZ, signal.SIG_IGN)
        target = work_directory / "fictional-output.bin"
        try:
            with target.open("wb") as output:
                output.write(b"x" * request["write_bytes"])
        except OSError as error:
            return {"blocked": error.errno == errno.EFBIG, "size": target.stat().st_size}
        return {"blocked": False, "size": target.stat().st_size}
    if mode == "descriptors":
        handles = []
        try:
            while True:
                handles.append(os.open(os.devnull, os.O_RDONLY))
        except OSError as error:
            return {"blocked": error.errno == errno.EMFILE, "opened": len(handles)}
        finally:
            for handle in handles:
                os.close(handle)
    if mode == "output":
        for descriptor in request["descriptors"]:
            os.write(descriptor, b"x" * request["write_bytes"])
        return {"done": True}
    if mode in {"cpu", "wait"}:
        if mode == "wait":
            signal.pause()
        while True:
            pass
    if mode == "descendant":
        reader, writer = os.pipe()
        pid = os.fork()
        if pid == 0:
            os.close(reader)
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
            null = os.open(os.devnull, os.O_WRONLY)
            os.dup2(null, 1)
            os.dup2(null, 2)
            Path(request["marker"]).write_text(str(os.getpid()), encoding="ascii")
            os.write(writer, b"ready")
            os.close(writer)
            while True:
                signal.pause()
        os.close(writer)
        assert os.read(reader, 5) == b"ready"
        os.close(reader)
        if request["parent_waits"]:
            signal.pause()
        return {"child": pid}
    if mode == "raise":
        print(request["private_text"], file=sys.stderr, flush=True)
        raise RuntimeError(request["private_text"])
    if mode == "stderr":
        os.write(2, b"Fictional diagnostic\n")
        return {"fictional": True}
    if mode == "invalid_json":
        # Parent accepts bounded JSON only, including when trusted code misbehaves.
        payloads = {
            "not-json": b"not-json\n",
            "list": b"[]",
            "duplicate": b'{"fictional":1,"fictional":2}',
            "nan": b'{"fictional":NaN}',
            "infinity": b'{"fictional":Infinity}',
            "utf8": b"\xff",
        }
        os.write(1, payloads[request["payload"]])
        os._exit(0)
    raise ValueError(json.dumps(request))
