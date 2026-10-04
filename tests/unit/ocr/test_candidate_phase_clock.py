"""Private measurement plumbing using fictional inputs, not SDK measurements."""

import io
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from coinpup_api.ocr.pdf_prepare import _phase_interval
from coinpup_api.ocr.recognize import failed_result

from scripts.ocr_benchmark import candidate_session as session
from scripts.ocr_benchmark.engine_environment import CandidateEnvironmentError
from scripts.ocr_benchmark.phase_clock import PhaseClockError, PhaseJournal


@pytest.fixture
def candidate(monkeypatch):
    calls = []
    adapter = SimpleNamespace(
        metadata={"profile": {"engine": "paddle"}}, close=lambda: calls.append("close")
    )
    monkeypatch.setattr(session, "_models", lambda *_: calls.append("models"))
    monkeypatch.setattr(session, "create_adapter", lambda *_: calls.append("init") or adapter)

    def source(item):
        calls.append(("source", item["id"]))
        if item.get("broken"):
            raise ValueError("fictional private source")
        return b"FICTIONAL"

    monkeypatch.setattr(session, "_source", source)
    return calls


def invoke(tmp_path, requests, clock=None):
    reader = io.BytesIO(b"".join(json.dumps(item).encode() + b"\n" for item in requests))
    writer = io.BytesIO()
    code = session.serve({"engine": "paddle"}, tmp_path, reader, writer, phase_clock=clock)
    return code, list(map(json.loads, writer.getvalue().splitlines()))


def request(identifier, **extras):
    return {"v": 1, "id": identifier, "media_type": "application/pdf", **extras}


def test_init_and_request_traces_are_separate_and_failed_source_has_real_end(
    tmp_path, monkeypatch, candidate
):
    observers = []

    def recognize(*_, page_done, _observe_native, _phase_observer):
        observers.append(_phase_observer)
        with _phase_interval(_phase_observer, "parse"):
            result = failed_result("fictional_terminal")
        return result

    monkeypatch.setattr(session, "recognize_document", recognize)
    with PhaseJournal(tmp_path / "phases.jsonl") as clock:
        code, frames = invoke(tmp_path, [request(0, broken=True), request(1), request(2)], clock)
        assert code == 0 and observers == [clock, clock]
        init_events = frames[0]["timings_ns"]["events"]
        assert [item["phase"] for item in init_events] == [
            "model_verify",
            "model_verify",
            "engine_init",
            "engine_init",
        ]
        first, second, third = [frame["result"] for frame in frames[1:]]
        assert first["reason"] == "source_invalid"
        assert [item["phase"] for item in first["timings_ns"]["events"]] == ["source", "source"]
        assert first["timings_ns"]["events"][-1]["details"] == {"outcome": "failed"}
        for result in (second, third):
            events = result["timings_ns"]["events"]
            assert [item["phase"] for item in events] == ["source", "source", "parse", "parse"]
            assert all(item["at_ns"] >= result["timings_ns"]["source"]["start"] for item in events)
        assert clock.events == init_events + sum(
            [result["timings_ns"]["events"] for result in (first, second, third)], []
        )
    assert candidate == ["models", "init", ("source", 0), ("source", 1), ("source", 2), "close"]


def test_disabled_measurement_passes_no_new_kwarg_and_preserves_result_shape(
    tmp_path, monkeypatch, candidate
):
    def recognize(*_, page_done, _observe_native):
        return failed_result("fictional_terminal")

    monkeypatch.setattr(session, "recognize_document", recognize)
    code, frames = invoke(tmp_path, [request(0)])
    assert code == 0
    assert frames[1]["result"]["reason"] == "fictional_terminal"
    assert "events" not in frames[0]["timings_ns"]
    assert "events" not in frames[1]["result"]["timings_ns"]


def test_failed_clock_cannot_be_hidden_by_recognizer_error_recovery(
    tmp_path, monkeypatch, candidate
):
    def recognize(*_, page_done, _observe_native, _phase_observer):
        try:
            _phase_observer("not_a_phase", None, "start", 1)
        except PhaseClockError:
            pass
        return failed_result("processing_failed")

    monkeypatch.setattr(session, "recognize_document", recognize)
    with PhaseJournal(tmp_path / "phases.jsonl") as clock:
        with pytest.raises(PhaseClockError, match="^phase_clock_invalid$"):
            invoke(tmp_path, [request(0)], clock)
        assert clock.failed
    assert candidate[-1] == "close"


def test_missing_model_trace_ends_failed_before_any_initializer(tmp_path, monkeypatch, candidate):
    def reject(*_):
        raise CandidateEnvironmentError("candidate_models_invalid")

    monkeypatch.setattr(session, "_models", reject)
    with PhaseJournal(tmp_path / "phases.jsonl") as clock:
        code, frames = invoke(tmp_path, [], clock)
        assert code == 1 and candidate == []
        assert frames == [{"v": 1, "event": "startup_failed", "reason": "candidate_models_invalid"}]
        assert [item["phase"] for item in clock.events] == ["model_verify", "model_verify"]
        assert clock.events[-1]["details"] == {"outcome": "failed"}


def test_cli_opens_and_closes_private_journal_without_owning_protocol_writer(tmp_path, monkeypatch):
    path, writer, seen = tmp_path / "phases.jsonl", io.BytesIO(), []
    monkeypatch.setattr(
        session.sys,
        "argv",
        [
            "candidate",
            "--profile",
            '{"engine":"paddle"}',
            "--preflight-report",
            "public-report.json",
            "--audit-output",
            "native.json",
            "--phase-clock-path",
            str(path),
        ],
    )
    monkeypatch.setattr(session.sys, "stdin", SimpleNamespace(buffer=io.BytesIO()))

    def serve(*_, phase_clock, **kwargs):
        assert kwargs["preflight_report"] == Path("public-report.json")
        phase_clock("source", None, "start", 1)
        seen.append(phase_clock)
        return 0

    monkeypatch.setattr(session, "serve", serve)
    assert session.main(protocol_writer=writer) == 0
    assert not writer.closed and json.loads(path.read_bytes())["edge"] == "start"
    with pytest.raises(PhaseClockError):
        seen[0]("source", None, "end", 2, {"outcome": "passed"})
