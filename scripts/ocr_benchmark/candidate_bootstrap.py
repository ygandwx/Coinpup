"""Standard-library boot gate: monitoring starts before importing candidate modules."""

import importlib
import json
import os
import sys

_GATE_BYTES = 1024


def _unique(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError
        value[key] = item
    return value


def _constant(value):
    raise ValueError


def _send(writer, frame):
    writer.write(json.dumps(frame, allow_nan=False).encode("utf8") + b"\n")
    writer.flush()


def _failed(writer, reason):
    try:
        _send(writer, {"v": 1, "event": "startup_failed", "reason": reason})
    except Exception:
        pass  # A broken protocol pipe still produces a reliable failure exit status.
    return 1


def run_gate(reader, writer):
    """Consume only the gate line; the candidate retains the same remaining binary stdin."""
    try:
        _send(writer, {"v": 1, "event": "boot", "pid": os.getpid()})
        data = reader.readline(_GATE_BYTES + 1)
        if not 1 <= len(data) <= _GATE_BYTES or not data.endswith(b"\n"):
            raise ValueError
        request = json.loads(
            data.decode("utf8"), object_pairs_hook=_unique, parse_constant=_constant
        )
        if (
            type(request) is not dict
            or request.keys() != {"v", "action"}
            or type(request["v"]) is not int
            or request["v"] != 1
            or request["action"] != "go"
        ):
            raise ValueError
    except Exception:
        return _failed(writer, "candidate_boot_invalid")
    try:
        candidate = importlib.import_module("scripts.ocr_benchmark.candidate_session")
        code = candidate.main(protocol_writer=writer)
        if type(code) is not int or not 0 <= code <= 255:
            raise ValueError
        return code
    except Exception:
        return _failed(writer, "candidate_boot_failed")


def main():
    descriptor = None
    try:
        sys.stdout.flush()
        descriptor = os.dup(sys.stdout.fileno())
        with os.fdopen(descriptor, "wb", buffering=0) as writer:
            descriptor = None  # The stream owns this one preserved protocol descriptor.
            os.dup2(sys.stderr.fileno(), sys.stdout.fileno())
            return run_gate(sys.stdin.buffer, writer)
    except Exception:
        if descriptor is not None:
            os.close(descriptor)
        try:
            print("candidate_boot_failed", file=sys.stderr)
        except Exception:
            pass
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
