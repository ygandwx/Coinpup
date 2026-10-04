"""Protocol and ownership contracts; no optional SDK or document recognition."""

import hashlib
import io
import json
import subprocess
import sys
from pathlib import Path

import pytest
from coinpup_api.ocr.engine_adapters import EngineError
from coinpup_api.ocr.recognize import failed_result

from scripts.ocr_benchmark import candidate_session as session


@pytest.fixture
def staged(tmp_path, monkeypatch):
    tmp_path.chmod(0o700)
    path = tmp_path / ("a" * 32 + ".pdf")
    content = b"EXPLICITLY FICTIONAL SOURCE BYTES"
    path.write_bytes(content)
    path.chmod(0o600)
    monkeypatch.setattr(session, "STAGING_ROOT", tmp_path)
    return {
        "v": 1,
        "id": 0,
        "media_type": "application/pdf",
        "source": {
            "path": str(path),
            "sha256": hashlib.sha256(content).hexdigest(),
            "byte_size": len(content),
        },
    }


@pytest.fixture
def candidate(monkeypatch):
    calls = []

    class Adapter:
        metadata = {"profile": {"engine": "paddle"}, "versions": {"fictional": "1"}}

        def close(self):
            calls.append("close")

    adapter = Adapter()
    monkeypatch.setattr(
        session, "_models", lambda directory, engine: calls.append(("models", engine))
    )

    def create(profile, directory):
        calls.append(("create", profile.copy(), directory))
        return adapter

    monkeypatch.setattr(session, "create_adapter", create)
    return adapter, calls


def invoke(requests, directory, *, raw=None):
    reader = io.BytesIO(
        raw if raw is not None else b"".join(json.dumps(item).encode() + b"\n" for item in requests)
    )
    writer = io.BytesIO()
    code = session.serve({"engine": "paddle"}, directory, reader, writer)
    return code, [json.loads(line) for line in writer.getvalue().splitlines()]


def test_real_missing_models_fail_before_any_initializer(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(session, "create_adapter", lambda *_: calls.append(True))
    code, frames = invoke([], tmp_path)
    assert code == 1 and calls == []
    assert frames == [{"v": 1, "event": "startup_failed", "reason": "candidate_models_invalid"}]


def test_one_adapter_and_ordered_request_ids_with_actual_original_bytes(
    staged, candidate, monkeypatch
):
    adapter, calls = candidate
    received = []

    def recognize(data, media, actual, *, page_done, _observe_native):
        received.append((data, media, actual))
        page_done(1)
        page_done(0)
        return failed_result("fictional_test", page_indices=(0, 1))

    monkeypatch.setattr(session, "recognize_document", recognize)
    code, frames = invoke([staged, {**staged, "id": 7}], Path(staged["source"]["path"]).parent)
    assert code == 0 and [call[0] if isinstance(call, tuple) else call for call in calls] == [
        "models",
        "create",
        "close",
    ]
    assert len(received) == 2 and all(
        item == (b"EXPLICITLY FICTIONAL SOURCE BYTES", "application/pdf", adapter)
        for item in received
    )
    assert [frame["event"] for frame in frames] == [
        "ready",
        "page_done",
        "page_done",
        "result",
        "page_done",
        "page_done",
        "result",
    ]
    assert [
        (frame["id"], frame["page_index"]) for frame in frames if frame["event"] == "page_done"
    ] == [(0, 1), (0, 0), (7, 1), (7, 0)]


@pytest.mark.parametrize("fault", ["hash", "size", "name", "scope", "extra", "mime"])
def test_source_admission_failure_does_not_call_recognition_and_next_request_works(
    staged, candidate, monkeypatch, fault
):
    bad = json.loads(json.dumps(staged))
    if fault == "hash":
        bad["source"]["sha256"] = "0" * 64
    elif fault == "size":
        bad["source"]["byte_size"] += 1
    elif fault == "name":
        bad["source"]["path"] = str(Path(bad["source"]["path"]).with_name("truth-template.pdf"))
    elif fault == "scope":
        bad["source"]["path"] = str(Path(bad["source"]["path"]).parent.parent / ("b" * 32 + ".pdf"))
    elif fault == "extra":
        bad["expected_fields"] = {"header.total": "1.00"}
    else:
        bad["media_type"] = "image/png"
    calls = []
    monkeypatch.setattr(
        session,
        "recognize_document",
        lambda *args, **kwargs: calls.append(args) or failed_result("fictional_test"),
    )
    code, frames = invoke([bad, {**staged, "id": 1}], Path(staged["source"]["path"]).parent)
    assert code == 0 and len(calls) == 1
    assert frames[1]["result"]["reason"] == "source_invalid"
    assert "truth-template" not in json.dumps(frames)
    assert candidate[1][-1] == "close"


@pytest.mark.parametrize(
    "raw",
    [
        b'{"id":0,"id":1}\n',
        b'{"id":NaN}\n',
        b'{"id":Infinity}\n',
        b'{"id":true}\n',
        b'{"id":-1}\n',
        b"[]\n",
        b"\xff\n",
        b'{"id":0}',
        b"x" * (1024**2 + 1) + b"\n",
    ],
    ids=["duplicate", "nan", "infinity", "bool", "negative", "array", "utf8", "partial", "large"],
)
def test_invalid_json_is_bounded_terminal_and_closes_the_session(candidate, tmp_path, raw):
    code, frames = invoke([], tmp_path, raw=raw)
    assert code == 1 and frames[-1] == {
        "v": 1,
        "event": "protocol_error",
        "reason": "request_invalid",
    }
    assert candidate[1][-1] == "close"


def test_duplicate_request_id_is_terminal_without_second_read(staged, candidate, monkeypatch):
    calls = []
    monkeypatch.setattr(
        session,
        "recognize_document",
        lambda *args, **kwargs: calls.append(True) or failed_result("fictional_test"),
    )
    code, frames = invoke([staged, staged], Path(staged["source"]["path"]).parent)
    assert code == 1 and calls == [True]
    assert frames[-1]["event"] == "protocol_error" and candidate[1][-1] == "close"


@pytest.mark.parametrize(
    "error,reason",
    [
        (MemoryError(), "resource_limit"),
        (RuntimeError("fictional secret/path"), "processing_failed"),
    ],
)
def test_failure_preserves_already_completed_physical_indices_and_next_request(
    staged, candidate, monkeypatch, error, reason
):
    attempts = []

    def recognize(*args, page_done, _observe_native):
        attempts.append(True)
        page_done(3)
        if len(attempts) == 1:
            raise error
        return failed_result("fictional_test", page_indices=(3,))

    monkeypatch.setattr(session, "recognize_document", recognize)
    code, frames = invoke([staged, {**staged, "id": 1}], Path(staged["source"]["path"]).parent)
    results = [frame["result"] for frame in frames if frame["event"] == "result"]
    assert code == 0 and results[0]["reason"] == reason
    assert [[page["page_index"] for page in result["pages"]] for result in results] == [[3], [3]]
    assert "fictional secret" not in json.dumps(frames) and candidate[1][-1] == "close"


@pytest.mark.parametrize(
    "error", [EngineError("engine_initialization_failed"), RuntimeError("fictional password/path")]
)
def test_startup_error_is_stable_and_never_echoes_exception(monkeypatch, tmp_path, error):
    monkeypatch.setattr(session, "_models", lambda *_: None)

    def create(*_):
        raise error

    monkeypatch.setattr(session, "create_adapter", create)
    code, frames = invoke([], tmp_path)
    assert code == 1 and frames == [
        {"v": 1, "event": "startup_failed", "reason": "engine_initialization_failed"}
    ]


def test_nonfinite_metadata_never_produces_ready_and_adapter_is_closed(candidate, tmp_path):
    candidate[0].metadata = {"invalid": float("nan")}
    code, frames = invoke([], tmp_path)
    assert code == 1 and frames[0]["event"] == "startup_failed"
    assert candidate[1][-1] == "close"


def test_closed_protocol_writer_still_disposes_the_adapter(staged, candidate):
    writer = io.BytesIO()
    writer.close()
    with pytest.raises(ValueError):
        session.serve(
            {"engine": "paddle"}, Path(staged["source"]["path"]).parent, io.BytesIO(), writer
        )
    assert candidate[1][-1] == "close"


def test_default_import_does_not_load_optional_ocr_or_model_libraries():
    source = Path(__file__).resolve().parents[3] / "services/api/src"
    code = (
        f"import sys; sys.path.insert(0, {str(source)!r}); "
        "import scripts.ocr_benchmark.candidate_session; "
        "assert not set(('PIL', 'pdfplumber', 'pypdfium2', 'paddle', 'paddleocr', 'cv2')) "
        "& set(sys.modules)"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, check=False, timeout=20
    )
    assert result.returncode == 0, result.stderr


def test_finish_audits_live_observation_union_before_close(
    staged, candidate, monkeypatch, tmp_path
):
    from scripts.ocr_benchmark import recognition_audit

    paths = iter(
        [
            {Path("/fictional/initial.so")},
            {Path("/fictional/live-only.so")},
            {Path("/fictional/final.so")},
        ]
    )
    monkeypatch.setattr(session, "_loaded_native", lambda: next(paths))
    observed = []

    def recognize(*args, page_done, _observe_native):
        _observe_native()
        page_done(0)
        return failed_result("fictional_test", page_indices=(0,))

    def audit(engine, preflight, actual, outpath, *, observation_complete):
        assert observation_complete is True
        assert candidate[1][-1] != "close"
        observed.append((engine, preflight, actual.copy(), outpath))
        return {
            "status": "passed",
            "reason": None,
            "native_count": len(actual),
            "report_sha256": "a" * 64,
        }

    monkeypatch.setattr(session, "recognize_document", recognize)
    monkeypatch.setattr(recognition_audit, "post_recognition_audit", audit)
    writer = io.BytesIO()
    reader = io.BytesIO(json.dumps(staged).encode() + b'\n{"v":1,"id":1,"action":"finish"}\n')
    code = session.serve(
        {"engine": "paddle"},
        tmp_path,
        reader,
        writer,
        preflight_report=tmp_path / "preflight.json",
        audit_output=tmp_path / "native.json",
    )
    frames = [json.loads(line) for line in writer.getvalue().splitlines()]
    assert code == 0 and frames[-1]["event"] == "audit_done" and candidate[1][-1] == "close"
    assert observed[0][2] == {
        Path("/fictional/initial.so"),
        Path("/fictional/live-only.so"),
        Path("/fictional/final.so"),
    }


def test_eof_cannot_claim_a_successful_configured_audit(candidate, monkeypatch, tmp_path):
    from scripts.ocr_benchmark import recognition_audit

    calls = []
    monkeypatch.setattr(session, "_loaded_native", lambda: [Path("/fictional/initial.so")])
    monkeypatch.setattr(
        recognition_audit, "post_recognition_audit", lambda *args, **kwargs: calls.append(True)
    )
    writer = io.BytesIO()
    code = session.serve(
        {"engine": "paddle"},
        tmp_path,
        io.BytesIO(),
        writer,
        preflight_report=tmp_path / "preflight.json",
        audit_output=tmp_path / "native.json",
    )
    assert code == 1 and calls == [] and candidate[1][-1] == "close"
    assert [json.loads(line)["event"] for line in writer.getvalue().splitlines()] == ["ready"]


def test_audit_failure_is_terminal_and_keeps_a_stable_reason(candidate, monkeypatch, tmp_path):
    from scripts.ocr_benchmark import recognition_audit

    monkeypatch.setattr(session, "_loaded_native", lambda: [Path("/fictional/initial.so")])

    def audit(*args, **kwargs):
        raise recognition_audit.RecognitionAuditError(
            "candidate_license_incomplete",
            {"status": "failed", "reason": "candidate_license_incomplete"},
        )

    monkeypatch.setattr(recognition_audit, "post_recognition_audit", audit)
    writer = io.BytesIO()
    code = session.serve(
        {"engine": "paddle"},
        tmp_path,
        io.BytesIO(b'{"v":1,"id":0,"action":"finish"}\n'),
        writer,
        preflight_report=tmp_path / "preflight.json",
        audit_output=tmp_path / "native.json",
    )
    frames = [json.loads(line) for line in writer.getvalue().splitlines()]
    assert code == 1 and frames[-1] == {
        "v": 1,
        "id": 0,
        "event": "audit_failed",
        "reason": "candidate_license_incomplete",
    }
    assert candidate[1][-1] == "close"


def test_real_cli_descriptor_keeps_native_noise_out_of_protocol(tmp_path):
    source = Path(__file__).resolve().parents[3] / "services/api/src"
    code = f"""
import sys, os
sys.path.insert(0, {str(source)!r})
from scripts.ocr_benchmark import candidate_session as session, recognition_audit
class Adapter:
    metadata = {{'profile': {{'engine': 'paddle'}}}}
    def close(self): os.write(1, b'FICTIONAL_NATIVE_CLOSE\\n')
def create(*args):
    os.write(1, b'FICTIONAL_NATIVE_INIT\\n')
    return Adapter()
session._models = lambda *args: None
session._loaded_native = lambda: [__import__('pathlib').Path('/fictional/initial.so')]
session.create_adapter = create
recognition_audit.post_recognition_audit = lambda *args, **kwargs: {{
    'status':'passed','reason':None,'native_count':0,'report_sha256':'a'*64}}
sys.argv = ['candidate-session','--profile','{{"engine":"paddle"}}',
    '--preflight-report','fictional-preflight.json','--audit-output','fictional-output.json']
raise SystemExit(session.main())
"""
    result = subprocess.run(
        [sys.executable, "-c", code],
        input=b'{"v":1,"id":0,"action":"finish"}\n',
        capture_output=True,
        check=False,
        timeout=20,
    )
    assert result.returncode == 0
    assert [json.loads(line)["event"] for line in result.stdout.splitlines()] == [
        "ready",
        "audit_done",
    ]
    assert b"FICTIONAL_NATIVE_INIT" in result.stderr and b"FICTIONAL_NATIVE_CLOSE" in result.stderr


def test_failed_live_observation_cannot_be_masked_by_later_success(
    staged, candidate, monkeypatch, tmp_path
):
    from scripts.ocr_benchmark import recognition_audit

    attempts = []

    def loaded():
        attempts.append(True)
        if len(attempts) == 2:
            raise OSError("fictional unavailable proc")
        return [Path("/fictional/initial.so" if len(attempts) == 1 else "/fictional/later.so")]

    def recognize(*args, page_done, _observe_native):
        with pytest.raises(OSError):
            _observe_native()
        page_done(0)
        return failed_result("processing_failed", page_indices=(0,))

    def audit(engine, preflight, paths, output, *, observation_complete):
        assert observation_complete is False
        assert paths == {Path("/fictional/initial.so"), Path("/fictional/later.so")}
        raise recognition_audit.RecognitionAuditError(
            "recognition_observation_incomplete", {"status": "failed"}
        )

    monkeypatch.setattr(session, "_loaded_native", loaded)
    monkeypatch.setattr(session, "recognize_document", recognize)
    monkeypatch.setattr(recognition_audit, "post_recognition_audit", audit)
    reader = io.BytesIO(json.dumps(staged).encode() + b'\n{"v":1,"id":1,"action":"finish"}\n')
    writer = io.BytesIO()
    code = session.serve(
        {"engine": "paddle"},
        tmp_path,
        reader,
        writer,
        preflight_report=tmp_path / "preflight.json",
        audit_output=tmp_path / "native.json",
    )
    frames = [json.loads(line) for line in writer.getvalue().splitlines()]
    assert code == 1 and len(attempts) == 3 and candidate[1][-1] == "close"
    assert frames[-1] == {
        "v": 1,
        "id": 1,
        "event": "audit_failed",
        "reason": "recognition_observation_incomplete",
    }
