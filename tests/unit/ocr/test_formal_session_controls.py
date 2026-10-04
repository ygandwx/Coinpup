"""Formal observation controls retain the bounded transport; no candidate executes."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts.ocr_benchmark import development_run as runner
from tests.unit.ocr.test_development_run import Process, Selector, Stream, rejected, session


def frame_bytes(value):
    return json.dumps(value).encode("utf8") + b"\n"


def test_phase_clock_is_only_an_explicit_cli_option_and_preserves_default(monkeypatch):
    commands, deadlines = [], []
    profile = runner.PROFILES[0]
    monkeypatch.setattr(runner.sys, "platform", "linux")
    monkeypatch.setattr(runner.uuid, "uuid4", lambda: SimpleNamespace(hex="f" * 32))
    monkeypatch.setattr(runner.time, "monotonic", lambda: 7)
    monkeypatch.setattr(runner.selectors, "DefaultSelector", Selector)
    monkeypatch.setattr(
        runner.subprocess, "Popen", lambda args, **kw: commands.append((args, kw)) or Process()
    )
    monkeypatch.setattr(
        runner.ContainerSession,
        "_frame",
        lambda self, deadline: (
            deadlines.append(deadline)
            or {"v": 1, "event": "ready", "metadata": {"profile": profile}}
        ),
    )
    values = [
        runner.ContainerSession(
            profile, Path("/fictional/staging"), Path("/fictional/assets"), "2,4", **options
        )
        for options in ({}, {"phase_clock": False}, {"phase_clock": True})
    ]
    assert commands[0] == commands[1]
    assert commands[2][0] == commands[0][0] + ["--phase-clock-path", "/audit/phases.jsonl"]
    assert commands[2][1] == commands[0][1]
    assert "scripts.ocr_benchmark.candidate_session" in commands[0][0]
    assert "--phase-clock-path" not in commands[0][0]
    assert deadlines == [127, 127, 127]
    assert all(
        value.metadata == {"profile": profile} and value.poll_hook is None for value in values
    )


def test_phase_clock_is_forwarded_to_bootstrap_without_changing_startup_gate(monkeypatch):
    from scripts.ocr_benchmark import resource_monitor

    process, commands, deadlines = Process(), [], []
    profile = runner.PROFILES[-1]
    frames = iter(
        [
            {"v": 1, "event": "boot", "pid": 17},
            {"v": 1, "event": "ready", "metadata": {"profile": profile}},
        ]
    )
    monkeypatch.setattr(runner.sys, "platform", "linux")
    monkeypatch.setattr(runner.time, "monotonic", lambda: 3)
    monkeypatch.setattr(runner.selectors, "DefaultSelector", Selector)
    monkeypatch.setattr(
        runner.subprocess, "Popen", lambda args, **kw: commands.append(args) or process
    )
    monkeypatch.setattr(
        runner.ContainerSession,
        "_frame",
        lambda self, deadline: deadlines.append(deadline) or next(frames),
    )
    samples = []
    monkeypatch.setattr(
        resource_monitor,
        "ResourceMonitor",
        lambda *args: SimpleNamespace(start=lambda: samples.append(process.stdin.getvalue())),
    )
    runner.ContainerSession(
        profile,
        Path("/fictional/staging"),
        Path("/fictional/assets"),
        "2,4",
        resource_output=Path("/fictional/resources.json"),
        phase_clock=True,
    )
    assert samples == [b""] and process.stdin.getvalue() == b'{"v":1,"action":"go"}\n'
    assert deadlines == [123, 123]
    assert "scripts.ocr_benchmark.candidate_bootstrap" in commands[0]
    assert commands[0][-2:] == ["--phase-clock-path", "/audit/phases.jsonl"]


def test_poll_hook_observes_every_frame_even_if_already_buffered():
    value, observed = session(), []
    frames = [{"v": 1, "event": "first"}, {"v": 1, "event": "second"}]
    value.buffer.extend(b"".join(frame_bytes(frame) for frame in frames))
    value.poll_hook = lambda current: observed.append((current, bytes(current.buffer)))
    assert [value._frame(100), value._frame(100)] == frames
    assert [item[0] for item in observed] == [value, value]
    assert observed[0][1] == b"".join(frame_bytes(frame) for frame in frames)
    assert observed[1][1] == frame_bytes(frames[1])


def test_polling_hook_time_consumes_original_deadline_instead_of_resetting_it(monkeypatch):
    value, clock, hooks, waits = session(), [5.0], [], []

    def hook(current):
        assert current is value
        hooks.append(clock[0])
        clock[0] += 0.01

    def select(timeout):
        waits.append(timeout)
        clock[0] += timeout
        return []

    value.poll_hook = hook
    monkeypatch.setattr(runner.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(value.selector, "select", select)
    with rejected("candidate_timeout"):
        value._frame(5.1)
    assert len(hooks) == 8
    assert max(waits) == pytest.approx(0.005)
    assert all(0 < wait <= 0.005 for wait in waits)
    assert 5.1 <= clock[0] <= 5.115


def test_request_publishes_same_monotonic_submission_before_write_and_to_hook(monkeypatch):
    value, seen, writes = session(), [], []
    ticks = iter([101, 190, 230, 350])
    monkeypatch.setattr(runner.time, "monotonic_ns", lambda: next(ticks))

    class ObservedInput(Stream):
        def write(self, data):
            writes.append((value.last_submitted_ns, json.loads(data)))
            return super().write(data)

    value.process.stdin = ObservedInput()
    value.poll_hook = lambda current: seen.append(current.last_submitted_ns)
    source = {"path": "/opt/staging/fictional.pdf", "sha256": "a" * 64, "byte_size": 123}
    for request_id, submitted, returned in ((1, 101, 190), (2, 230, 350)):
        output = {"pages": [], "status": "manual", "reason": "fictional_review"}
        value.buffer.extend(
            frame_bytes({"v": 1, "id": request_id, "event": "result", "result": output})
        )
        result = value.request(source, "application/pdf")
        assert result == {"output": output, "submitted_ns": submitted, "returned_ns": returned}
        assert value.last_submitted_ns == submitted
        assert writes[-1] == (
            submitted,
            {"v": 1, "id": request_id, "source": source, "media_type": "application/pdf"},
        )
    assert seen == [101, 230]


@pytest.mark.parametrize(
    "error", [runner.DevelopmentError("candidate_timeout"), RuntimeError("hook")]
)
def test_hook_exception_keeps_submission_evidence_and_never_returns_buffered_success(
    monkeypatch, error
):
    value, calls = session(), []
    buffered = frame_bytes({"v": 1, "id": 1, "event": "result", "result": {"pages": []}})
    value.buffer.extend(buffered)
    monkeypatch.setattr(runner.time, "monotonic_ns", lambda: calls.append("clock") or 123)

    def hook(current):
        assert current.last_submitted_ns == 123
        assert json.loads(current.process.stdin.getvalue())["id"] == 1
        raise error

    value.poll_hook = hook
    with pytest.raises(type(error)) as caught:
        value.request({"path": "/opt/staging/fictional.pdf"}, "application/pdf")
    assert caught.value is error
    assert calls == ["clock"] and value.last_submitted_ns == 123
    assert bytes(value.buffer) == buffered and value.audit_finished is False


def test_request_hook_activity_without_page_done_does_not_extend_request(monkeypatch):
    value, clock, calls, waits = session(), [10.0], [], []
    monkeypatch.setattr(runner.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(runner.time, "monotonic_ns", lambda: calls.append("clock") or 100)

    def hook(current):
        assert current.last_submitted_ns == 100
        clock[0] += 20

    def select(timeout):
        waits.append(timeout)
        clock[0] += timeout
        return []

    value.poll_hook = hook
    monkeypatch.setattr(value.selector, "select", select)
    with rejected("candidate_timeout"):
        value.request({"path": "/opt/staging/fictional.pdf"}, "application/pdf")
    assert waits == [0.005, 0.005]
    assert clock[0] == pytest.approx(70.01)
    assert calls == ["clock"] and value.last_submitted_ns == 100


@pytest.mark.parametrize("excess,newline", [(0, True), (1, True), (1, False)])
def test_phase_poll_hook_cannot_bypass_existing_stdout_frame_limit(excess, newline):
    value, calls = session(), []
    prefix, suffix = b'{"v":1,"value":"', b'"}'
    payload = prefix + b"x" * (runner.FRAME_BYTES + excess - len(prefix) - len(suffix)) + suffix
    value.buffer.extend(payload + (b"\n" if newline else b""))
    value.poll_hook = lambda current: calls.append(current)
    if excess:
        with rejected("candidate_output_limit"):
            value._frame(100)
    else:
        assert value._frame(100)["value"] == "x" * (runner.FRAME_BYTES - len(prefix) - len(suffix))
    assert calls == [value]
