"""Raw clock admission and durable boundaries; no recognition or measured SDK claims."""

import json
import os
import stat

import pytest

from scripts.ocr_benchmark import phase_clock
from scripts.ocr_benchmark.phase_clock import PhaseClockError, PhaseJournal


def start(clock, tick=1):
    clock(
        "sdk_recognize",
        0,
        "start",
        tick,
        {
            "raster_rgb_sha256": "a" * 64,
            "width": 12,
            "height": 8,
        },
    )


def test_start_is_readable_before_sdk_finishes_and_snapshot_cannot_mutate_it(tmp_path):
    path = tmp_path / "phases.jsonl"
    with PhaseJournal(path) as clock:
        start(clock)
        records = [json.loads(line) for line in path.read_bytes().splitlines()]
        assert records == clock.events and len(records) == 1
        assert records[0]["edge"] == "start" and records[0]["at_ns"] == 1
        snapshot = clock.events
        snapshot[0]["details"]["width"] = 999
        snapshot[0]["phase"] = "secret"
        assert clock.events[0]["details"]["width"] == 12
        assert clock.events[0]["phase"] == "sdk_recognize"
        if os.name == "posix":
            assert stat.S_IMODE(path.stat().st_mode) == 0o644
        clock("sdk_recognize", 0, "end", 2, {"outcome": "failed"})
    assert [item["edge"] for item in map(json.loads, path.read_bytes().splitlines())] == [
        "start",
        "end",
    ]
    assert not clock.failed


@pytest.mark.parametrize(
    "args",
    [
        ("unknown", None, "start", 1, None),
        ("source", None, "progress", 1, None),
        ("source", True, "start", 1, None),
        ("source", 50, "start", 1, None),
        ("source", None, "start", True, None),
        ("source", None, "start", -1, None),
        ("source", None, "start", 2**63, None),
        ("source", None, "start", 1, {"filename": "fictional-private.pdf"}),
        ("source", None, "end", 1, {"outcome": "passed", "raw_text": "secret"}),
        ("source", None, "end", 1, {}),
        (
            "sdk_recognize",
            0,
            "start",
            1,
            {"raster_rgb_sha256": "a" * 64, "width": True, "height": 8},
        ),
        ("sdk_recognize", 0, "start", 1, {"raster_rgb_sha256": "A" * 64, "width": 12, "height": 8}),
        (
            "sdk_recognize",
            0,
            "start",
            1,
            {"raster_rgb_sha256": "a" * 64, "width": 10000, "height": 10000},
        ),
    ],
)
def test_invalid_events_fail_permanently_without_echoing_input(tmp_path, args):
    path = tmp_path / "phases.jsonl"
    with PhaseJournal(path) as clock:
        with pytest.raises(PhaseClockError, match="^phase_clock_invalid$"):
            clock(*args)
        assert path.read_bytes() == b"" and clock.events == [] and clock.failed
        with pytest.raises(PhaseClockError):
            start(clock)


def test_time_cannot_go_backwards_and_limit_does_not_invent_a_terminal_end(tmp_path):
    path = tmp_path / "backwards.jsonl"
    with PhaseJournal(path) as clock:
        start(clock, 20)
        with pytest.raises(PhaseClockError):
            clock("sdk_recognize", 0, "end", 19, {"outcome": "passed"})
        assert clock.events[0]["edge"] == "start" and len(clock.events) == 1
    path = tmp_path / "bounded.jsonl"
    with PhaseJournal(path) as clock:
        for tick in range(500):
            clock("source", None, "start", tick)
        with pytest.raises(PhaseClockError):
            clock("source", None, "end", 500, {"outcome": "passed"})
        assert len(clock.events) == 500
    assert len(path.read_bytes().splitlines()) == 500


def test_partial_os_writes_are_completed_and_io_failure_stays_failed(tmp_path, monkeypatch):
    original = os.write
    path = tmp_path / "partial.jsonl"
    with PhaseJournal(path) as clock:
        monkeypatch.setattr(phase_clock.os, "write", lambda fd, data: original(fd, data[:7]))
        start(clock)
        assert json.loads(path.read_bytes()) == clock.events[0]

        def broken(*_):
            raise OSError("fictional secret path")

        monkeypatch.setattr(phase_clock.os, "write", broken)
        with pytest.raises(PhaseClockError, match="^phase_clock_invalid$"):
            clock("sdk_recognize", 0, "end", 2, {"outcome": "failed"})
        monkeypatch.setattr(phase_clock.os, "write", original)
        with pytest.raises(PhaseClockError):
            clock("sdk_recognize", 0, "end", 3, {"outcome": "passed"})
        assert len(clock.events) == 1


def test_existing_output_and_missing_parent_cannot_be_reused(tmp_path):
    path = tmp_path / "phases.jsonl"
    path.write_bytes(b"original audit evidence")
    for invalid in (path, tmp_path / "missing" / "phases.jsonl"):
        with pytest.raises(PhaseClockError, match="^phase_clock_invalid$"):
            PhaseJournal(invalid)
    assert path.read_bytes() == b"original audit evidence"


def test_close_is_idempotent_but_events_cannot_be_written_after_close(tmp_path):
    clock = PhaseJournal(tmp_path / "phases.jsonl")
    start(clock)
    clock.close()
    clock.close()
    with pytest.raises(PhaseClockError):
        start(clock, 2)
    assert len(clock.events) == 1


@pytest.mark.parametrize("attempted", [False, True])
@pytest.mark.parametrize("outcome", ["passed", "failed"])
def test_render_allocation_evidence_is_preserved_without_mutable_aliases(
    tmp_path, attempted, outcome
):
    path = tmp_path / "render.jsonl"
    details = {"outcome": outcome, "bitmap_allocation_attempted": attempted}
    with PhaseJournal(path) as clock:
        clock("pdf_render", 0, "start", 1)
        clock("pdf_render", 0, "end", 2, details)
        details["bitmap_allocation_attempted"] = not attempted
        assert clock.events[-1]["details"]["bitmap_allocation_attempted"] is attempted
    assert json.loads(path.read_bytes().splitlines()[-1])["details"] == {
        "outcome": outcome,
        "bitmap_allocation_attempted": attempted,
    }


@pytest.mark.parametrize(
    "phase,edge,attempted,extra",
    [
        ("pdf_render", "end", 0, {}),
        ("pdf_render", "end", 1, {}),
        ("pdf_render", "end", None, {}),
        ("pdf_render", "end", "false", {}),
        ("pdf_render", "end", False, {"raw_text": "fictional-private"}),
        ("pdf_render", "start", False, {}),
        ("source", "end", False, {}),
        ("sdk_recognize", "end", False, {}),
        ("rgb_materialize", "end", False, {}),
    ],
)
def test_allocation_evidence_cannot_broaden_other_phase_contracts(
    tmp_path, phase, edge, attempted, extra
):
    path = tmp_path / "invalid.jsonl"
    details = {"outcome": "failed", "bitmap_allocation_attempted": attempted, **extra}
    with PhaseJournal(path) as clock:
        with pytest.raises(PhaseClockError, match="^phase_clock_invalid$"):
            clock(phase, 0, edge, 1, details)
        assert clock.failed and clock.events == [] and path.read_bytes() == b""
