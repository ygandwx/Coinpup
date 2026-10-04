"""Bounded offline development session; candidate input never includes scoring answers."""

import argparse
import json
import os
import sys
import time
from contextlib import nullcontext
from pathlib import Path

from coinpup_api.ocr.engine_adapters import EngineError, create_adapter
from coinpup_api.ocr.isolation import _json_value, _reject_constant, _unique_object
from coinpup_api.ocr.pdf_prepare import _phase_interval
from coinpup_api.ocr.processor import _read_source, _request
from coinpup_api.ocr.recognize import _ENGINE_REASONS, failed_result, recognize_document

from scripts.ocr_benchmark.engine_environment import (
    CandidateEnvironmentError,
    _loaded_native,
    _models,
)

STAGING_ROOT = Path("/opt/staging")
_LINE_BYTES = 1048576


def _decode(data):
    value = json.loads(
        data.decode("utf8"), object_pairs_hook=_unique_object, parse_constant=_reject_constant
    )
    _json_value(value)
    return value


def _send(writer, frame):
    encoded = json.dumps(frame, ensure_ascii=False, allow_nan=False).encode("utf8") + b"\n"
    if len(encoded) > _LINE_BYTES:
        raise ValueError
    writer.write(encoded)
    writer.flush()


def _source(request):
    if type(request) is not dict or request.keys() != {"v", "id", "source", "media_type"}:
        raise ValueError
    if type(request["v"]) is not int or request["v"] != 1:
        raise ValueError
    if type(request["id"]) is not int or not 0 <= request["id"] < 2**63:
        raise ValueError
    media = request["media_type"]
    if media not in ("application/pdf", "image/jpeg", "image/png", "image/webp"):
        raise ValueError
    options = {"version": 1, "action": "prepare_pdf", "source": request["source"]}
    if media != "application/pdf":
        options.update(action="prepare_image", media_type=media)
    source, path, *_ = _request(options)
    if path.parent != STAGING_ROOT:
        raise ValueError
    return _read_source(source, path)


def serve(
    profile,
    assets_dir,
    reader,
    writer,
    *,
    preflight_report=None,
    audit_output=None,
    phase_clock=None,
):
    """One immutable profile and adapter; every complete input receives one terminal frame."""
    initialization = {"start": time.monotonic_ns()}
    adapter = None
    ready = False
    observed = set()
    observation_failed = False

    def observe():
        nonlocal observation_failed
        try:
            paths = _loaded_native()
            if not paths:
                raise ValueError
            observed.update(paths)
        except Exception:
            observation_failed = True
            raise

    try:
        if type(profile) is not dict or profile.get("engine") not in ("paddle", "tesseract"):
            raise EngineError("engine_profile_invalid")
        initialization["model_verification_start"] = time.monotonic_ns()
        with _phase_interval(phase_clock, "model_verify"):
            _models(Path(assets_dir), profile["engine"])
        initialization["model_verification_end"] = time.monotonic_ns()
        initialization["adapter_init_start"] = time.monotonic_ns()
        with _phase_interval(phase_clock, "engine_init"):
            adapter = create_adapter(profile, Path(assets_dir))
        initialization["adapter_init_end"] = time.monotonic_ns()
        if preflight_report is not None and audit_output is not None:
            observe()
        metadata = adapter.metadata
        initialization["end"] = time.monotonic_ns()
        if phase_clock is not None:
            initialization["events"] = phase_clock.events
        _send(
            writer,
            {"v": 1, "event": "ready", "metadata": metadata, "timings_ns": initialization},
        )
        ready = True
    except (EngineError, CandidateEnvironmentError) as error:
        reason = (
            (error.reason if error.reason in _ENGINE_REASONS else "engine_initialization_failed")
            if isinstance(error, EngineError)
            else "candidate_models_invalid"
        )
        _send(writer, {"v": 1, "event": "startup_failed", "reason": reason})
        return 1
    except Exception:
        _send(writer, {"v": 1, "event": "startup_failed", "reason": "engine_initialization_failed"})
        return 1
    finally:
        if adapter is not None and not ready:
            adapter.close()
    try:
        last_id = -1
        while data := reader.readline(_LINE_BYTES + 1):
            request = None
            try:
                if len(data) > _LINE_BYTES or not data.endswith(b"\n"):
                    raise ValueError
                request = _decode(data)
                identifier = request.get("id") if type(request) is dict else None
                if type(identifier) is not int or not last_id < identifier < 2**63:
                    raise ValueError
                last_id = identifier
            except (ValueError, TypeError, UnicodeError, RecursionError, OverflowError):
                _send(writer, {"v": 1, "event": "protocol_error", "reason": "request_invalid"})
                return 1
            if request.get("action") == "finish":
                if (
                    request.keys() != {"v", "id", "action"}
                    or type(request["v"]) is not int
                    or request["v"] != 1
                ):
                    _send(writer, {"v": 1, "event": "protocol_error", "reason": "request_invalid"})
                    return 1
                from scripts.ocr_benchmark.recognition_audit import (
                    RecognitionAuditError,
                    post_recognition_audit,
                )

                try:
                    summary = post_recognition_audit(
                        profile["engine"],
                        preflight_report,
                        observed,
                        audit_output,
                        observation_complete=not observation_failed,
                    )
                except RecognitionAuditError as error:
                    _send(
                        writer,
                        {"v": 1, "id": identifier, "event": "audit_failed", "reason": error.reason},
                    )
                    return 1
                except Exception:
                    _send(
                        writer,
                        {
                            "v": 1,
                            "id": identifier,
                            "event": "audit_failed",
                            "reason": "recognition_audit_failed",
                        },
                    )
                    return 1
                _send(writer, {"v": 1, "id": identifier, "event": "audit_done", "summary": summary})
                return 0
            first_event = 0 if phase_clock is None else len(phase_clock.events)
            source_start = time.monotonic_ns()
            try:
                try:
                    with _phase_interval(phase_clock, "source"):
                        original = _source(request)
                finally:
                    source_end = time.monotonic_ns()
            except (ValueError, TypeError, OSError, RuntimeError, RecursionError, OverflowError):
                result = failed_result("source_invalid")
            else:
                completed = set()

                def progress(index, identifier=identifier, completed=completed):
                    if type(index) is not int or not 0 <= index < 50 or index in completed:
                        raise ValueError
                    completed.add(index)
                    _send(
                        writer,
                        {"v": 1, "id": identifier, "event": "page_done", "page_index": index},
                    )

                try:
                    phase_options = {} if phase_clock is None else {"_phase_observer": phase_clock}
                    result = recognize_document(
                        original,
                        request["media_type"],
                        adapter,
                        page_done=progress,
                        _observe_native=observe if preflight_report is not None else None,
                        **phase_options,
                    )
                    if preflight_report is not None:
                        observe()
                except MemoryError:
                    result = failed_result("resource_limit", page_indices=completed)
                except Exception:
                    result = failed_result("processing_failed", page_indices=completed)
            result["timings_ns"]["source"] = {"start": source_start, "end": source_end}
            if phase_clock is not None:
                if phase_clock.failed:
                    from scripts.ocr_benchmark.phase_clock import PhaseClockError

                    raise PhaseClockError()
                result["timings_ns"]["events"] = phase_clock.events[first_event:]
            _send(writer, {"v": 1, "id": identifier, "event": "result", "result": result})
        return int(preflight_report is not None or audit_output is not None)
    finally:
        adapter.close()


def main(*, protocol_writer=None):
    parser = argparse.ArgumentParser(description=__doc__)
    profile = parser.add_mutually_exclusive_group(required=True)
    profile.add_argument("--profile")
    profile.add_argument("--profile-path", type=Path)
    parser.add_argument("--assets-dir", type=Path, default=Path("/opt/assets"))
    parser.add_argument("--preflight-report", type=Path, required=True)
    parser.add_argument("--audit-output", type=Path, required=True)
    parser.add_argument("--phase-clock-path", type=Path)
    arguments = parser.parse_args()
    # A bootstrap owns its supplied descriptor and has already redirected native stdout.
    if protocol_writer is None:
        sys.stdout.flush()
        writer_context = os.fdopen(os.dup(sys.stdout.fileno()), "wb", buffering=0)
    else:
        writer_context = nullcontext(protocol_writer)
    with writer_context as writer:
        if protocol_writer is None:
            os.dup2(sys.stderr.fileno(), sys.stdout.fileno())
        try:
            if arguments.profile_path:
                with arguments.profile_path.open("rb") as stream:
                    data = stream.read(4097)
            else:
                data = arguments.profile.encode("utf8")
            if len(data) > 4096:
                raise ValueError
            selected = _decode(data)
        except (ValueError, TypeError, OSError, UnicodeError, RecursionError):
            _send(writer, {"v": 1, "event": "startup_failed", "reason": "engine_profile_invalid"})
            return 1
        try:
            phase_context = nullcontext(None)
            if arguments.phase_clock_path is not None:
                from scripts.ocr_benchmark.phase_clock import PhaseJournal

                phase_context = PhaseJournal(arguments.phase_clock_path)
            with phase_context as phase_clock:
                return serve(
                    selected,
                    arguments.assets_dir,
                    sys.stdin.buffer,
                    writer,
                    preflight_report=arguments.preflight_report,
                    audit_output=arguments.audit_output,
                    **({} if phase_clock is None else {"phase_clock": phase_clock}),
                )
        except Exception:
            print("processing_failed", file=sys.stderr)
            return 1


if __name__ == "__main__":
    raise SystemExit(main())
