"""Fictional orchestration evidence; no Docker, engines or formal inputs execute here."""

import hashlib
import json
from copy import deepcopy
from types import SimpleNamespace

import pytest

from scripts.ocr_benchmark import formal_run as run
from scripts.ocr_benchmark.development_run import DevelopmentError

SHA = "a" * 64
ERRORS = (
    "damaged",
    "encrypted",
    "page_limit",
    "pixel_limit",
    "stream_limit",
    "garbled_layer",
    "missing_model",
    "timeout",
)


def case(group, number=0, **extra):
    return {
        "id": f"{group}-{number:02}",
        "group": group,
        "media_type": "application/pdf",
        "raster_sha256": [SHA],
        "expected_fields": {"header.total": "1.00"},
        "reference_text": "FICTIONAL",
        **extra,
    }


def output(item):
    result = {
        "version": 1,
        "status": "processed",
        "reason": None,
        "parsed": {
            "fields": [{"path": "header.total", "value": "1.00", "status": "certain"}],
            "rows": [],
            "diagnostics": [],
        },
        "raw_text": "FICTIONAL",
        "pages": [
            {
                "page_index": 0,
                "layer": "present" if item["group"] == "text" else "absent",
                "route": "extract" if item["group"] == "text" else "render",
                "raster_rgb_sha256": None if item["group"] == "text" else SHA,
                "reason_code": None,
            }
        ],
        "timings_ns": {"prepare": 2, "recognize": 3, "parse": 1},
    }
    if item["group"] == "error":
        reasons = {
            "encrypted": "encrypted_pdf",
            "page_limit": "probe_limit",
            "pixel_limit": "prepare_limit",
            "stream_limit": "probe_limit",
            "garbled_layer": "unusable_text",
            "missing_model": "candidate_models_invalid",
            "timeout": "candidate_timeout",
            "damaged": "invalid_pdf",
        }
        result.update(status="manual", reason=reasons[item["error_kind"]], raw_text="")
        result["parsed"]["fields"] = []
        result["pages"][0].update(
            route="manual",
            raster_rgb_sha256=None,
            layer="present" if item["error_kind"] == "garbled_layer" else "unknown",
        )
    return result


def record(item, repeat=0):
    return {
        "case_id": item["id"],
        "round": repeat,
        "submitted_ns": 100,
        "returned_ns": 110 + repeat,
        "output": output(item),
    }


def resources():
    return {
        "status": "complete",
        "samples": [{"sample_end_ns": 4}],
        "same_window_rss_peak_bytes": 16,
        "final": {"memory_peak_bytes": 32},
        "cpu_usage_delta_usec": 2,
        "sample_gaps_ns": [10],
    }


@pytest.fixture
def matrix(tmp_path, monkeypatch):
    cases = [
        case(group, number)
        for group, count in (("text", 12), ("ocr", 24), ("degraded", 12))
        for number in range(count)
    ]
    for number, kind in enumerate(ERRORS):
        cases.append(
            case(
                "error",
                number,
                error_kind=kind,
                runtime_scenario={"kind": kind} if kind in ("missing_model", "timeout") else None,
            )
        )
    development = [case("development", n, template_id=f"D0{n + 1}") for n in range(4)]
    manifest = {"cases": list(reversed(cases)), "development": list(reversed(development))}
    profiles = {
        "tesseract": {"engine": "tesseract", "model_set": "fast", "psm": 6},
        "paddle": {"engine": "paddle"},
    }
    calls = []
    monkeypatch.setattr(run.sys, "platform", "linux")
    monkeypatch.setattr(run.os, "sched_getaffinity", lambda *_: {8, 4, 2}, raising=False)
    monkeypatch.setattr(run, "load_and_verify", lambda *_: {"profiles": profiles})
    monkeypatch.setattr(run, "verify_corpus", lambda *_: manifest)
    monkeypatch.setattr(run, "host_facts", lambda: {"kind": "fictional unit host"})

    def stage(_, items, directory):
        calls.append(("stage", [item["id"] for item in items]))
        return tmp_path / "staging", {item["id"]: {} for item in items}

    monkeypatch.setattr(run, "_stage", stage)

    def episode(profile, batch, *, warm, repeat, label, context):
        calls.append(
            (
                label,
                profile.copy(),
                [item["id"] for item in batch],
                [item["template_id"] for item in warm],
                repeat,
                context[-1],
            )
        )
        return {
            "started_ns": 1,
            "ready_ns": 3,
            "initialization_timings_ns": {"start": 1, "end": 2},
            "records": [record(item, repeat) for item in batch],
            "resources": resources(),
        }

    monkeypatch.setattr(run, "episode", episode)
    monkeypatch.setattr(run, "missing_model_case", lambda item, *_: record(item))
    monkeypatch.setattr(
        run,
        "timeout_case",
        lambda _, item, **kwargs: {"record": record(item), "resources": resources()},
    )
    return SimpleNamespace(
        cases=cases,
        development=development,
        manifest=manifest,
        profiles=profiles,
        calls=calls,
        output=tmp_path / "out",
        corpus=tmp_path / "corpus",
        assets=tmp_path / "assets",
        selection=tmp_path / "selection.json",
    )


def invoke(matrix):
    return run.run_formal(matrix.corpus, matrix.assets, matrix.output, matrix.selection)


def test_exact_matrix_paired_orders_fixed_warming_and_only_round_zero_quality(matrix, capsys):
    result = invoke(matrix)
    calls = matrix.calls[1:]
    assert [call[0] for call in calls[:2]] == ["text-tesseract", "text-paddle"]
    for prefix, start in (("cold", 2), ("hot", 12)):
        paired = calls[start : start + 10]
        for number in range(5):
            engines = ("tesseract", "paddle") if number % 2 == 0 else ("paddle", "tesseract")
            assert [row[0] for row in paired[number * 2 : number * 2 + 2]] == [
                f"{prefix}-{number}-{engine}" for engine in engines
            ]
            for row in paired[number * 2 : number * 2 + 2]:
                assert row[3] == ([] if prefix == "cold" else ["D01", "D02"])
                assert len(row[2]) == (1 if prefix == "cold" else 24)
                assert row[4] == (0 if prefix == "cold" else number)
                if prefix == "cold":
                    assert row[2] == [matrix.development[0]["id"]]
    assert [row[0] for row in calls[-2:]] == ["aux-tesseract", "aux-paddle"]
    assert all(len(row[2]) == 18 and row[3] == ["D01", "D02"] for row in calls[-2:])
    assert all(row[-1] == "2,4" and row[1] == matrix.profiles[row[1]["engine"]] for row in calls)
    assert all(row[2] == sorted(row[2]) for row in calls if row[0].startswith("hot"))
    saved = json.loads((matrix.output / "experiment.json").read_text())
    assert saved["status"] == "complete" and result["decision"]["selected"] == "tesseract"
    for engine in matrix.profiles:
        rows = saved["records"][engine]
        assert len(rows) == 152 and len(saved["cold"][engine]) == 5
        assert {item["id"] for item in matrix.cases} <= {row["case_id"] for row in rows}
        summary = result["candidates"][engine]
        assert summary["groups"]["ocr"]["T"] == 24
        assert summary["groups"]["text"]["T"] == 12
        assert summary["groups"]["degraded"]["T"] == 12 and summary["groups"]["error"]["T"] == 8
        assert summary["hot_latencies"]["all"]["N"] == 120
    assert "OCR_FORMAL_SUMMARY_V1=" in capsys.readouterr().out


@pytest.mark.parametrize(
    "fault,reason",
    [("matrix", "frozen_corpus_shape_invalid"), ("development", "development_inputs_invalid")],
)
def test_invalid_matrix_stops_before_any_container_or_staging(matrix, fault, reason):
    if fault == "matrix":
        matrix.manifest["cases"].pop()
    else:
        matrix.manifest["development"][0]["template_id"] = "D05"
    with pytest.raises(DevelopmentError) as caught:
        invoke(matrix)
    assert caught.value.reason == reason and matrix.calls == [] and not matrix.output.exists()


def test_text_failure_stops_before_cold_or_hot_and_saves_partial_denominator(matrix, monkeypatch):
    original = run.episode

    def incorrect(*args, **kwargs):
        report = original(*args, **kwargs)
        report["records"][0]["output"]["parsed"]["fields"][0]["value"] = "2.00"
        return report

    monkeypatch.setattr(run, "episode", incorrect)
    with pytest.raises(DevelopmentError) as caught:
        invoke(matrix)
    assert caught.value.reason == "text_gate_failed"
    saved = json.loads((matrix.output / "experiment.json").read_text())
    assert saved["status"] == "partial" and len(saved["records"]["tesseract"]) == 12
    assert [row[0] for row in matrix.calls[1:]] == ["text-tesseract"]
    assert not (matrix.output / "summary.json").exists()


@pytest.mark.parametrize(
    "failed_engines,selected",
    [
        (("paddle", "tesseract"), None),
        (("paddle",), "tesseract"),
        (("tesseract",), "paddle"),
    ],
)
def test_qualification_uses_each_frozen_candidate_without_relaxing_threshold(
    matrix, monkeypatch, capsys, failed_engines, selected
):
    original = run.episode

    def below_gate(profile, *args, **kwargs):
        report = original(profile, *args, **kwargs)
        if kwargs["label"].startswith("hot-") and profile["engine"] in failed_engines:
            for row in report["records"][:2]:
                row["output"]["parsed"]["fields"][0]["value"] = "2.00"
        return report

    monkeypatch.setattr(run, "episode", below_gate)
    result = invoke(matrix)
    assert result["decision"]["selected"] == selected
    assert result["decision"]["stop"] is (selected is None)
    for engine in failed_engines:
        assert result["candidates"][engine]["groups"]["ocr"]["C"] == 22
        assert result["candidates"][engine]["groups"]["ocr"]["T"] == 24
    assert "OCR_FORMAL_SUMMARY_V1=" in capsys.readouterr().out


def test_broken_episode_restores_attempted_slots_and_all_planned_denominators(matrix, monkeypatch):
    original = run.episode

    def interrupted(profile, batch, **kwargs):
        if kwargs["label"] != "hot-0-tesseract":
            return original(profile, batch, **kwargs)
        rows = [record(batch[0]), record(batch[1])]
        rows[1].update(
            output=run.failed_result("candidate_timeout"),
            operational_failure=True,
            native_audit_complete=False,
            returned_ns=60000000100,
        )
        run._save(
            matrix.output / (kwargs["label"] + ".json"),
            {
                "profile": profile,
                "records": rows,
                "status": "partial",
                "reason": "candidate_timeout",
            },
        )
        raise DevelopmentError("candidate_timeout")

    monkeypatch.setattr(run, "episode", interrupted)
    with pytest.raises(DevelopmentError):
        invoke(matrix)
    saved = json.loads((matrix.output / "experiment.json").read_text())
    assert saved["decision"] == {"selected": None, "stop": True, "reason": "experiment_incomplete"}
    assert (
        len(saved["planned_documents"]) == 56
        and sum(row["T"] for row in saved["planned_documents"]) == 56
    )
    assert len(saved["attempted_slots"]["tesseract"]) == 14
    assert len(saved["attempted_slots"]["paddle"]) == 12
    assert saved["records"]["tesseract"][-1]["output"]["parsed"]["fields"] == []
    assert saved["records"]["tesseract"][-1]["returned_ns"] == 60000000100
    assert not (matrix.output / "summary.json").exists()


def test_later_round_cannot_rescue_first_or_authorize_unstable_choice(matrix, monkeypatch):
    original = run.episode

    def unstable(*args, **kwargs):
        report = original(*args, **kwargs)
        if kwargs["label"] == "hot-0-tesseract":
            report["records"][0]["output"]["parsed"]["fields"][0]["value"] = "2.00"
        return report

    monkeypatch.setattr(run, "episode", unstable)
    with pytest.raises(DevelopmentError) as caught:
        invoke(matrix)
    assert caught.value.reason == "candidate_output_unstable"
    saved = json.loads((matrix.output / "experiment.json").read_text())
    assert saved["status"] == "partial" and len(saved["records"]["tesseract"]) == 152
    assert saved["decision"]["selected"] is None and saved["decision"]["stop"] is True
    assert saved["candidates"]["tesseract"]["groups"]["ocr"]["C"] == 23
    assert saved["candidates"]["tesseract"]["groups"]["ocr"]["T"] == 24
    assert not (matrix.output / "summary.json").exists()


class FakeSession:
    started_ns, gate_sent_ns, ready_ns = 1, 5, 9
    initialization_timings = {"start": 6, "end": 8}
    metadata, stderr, stderr_bytes = {}, bytearray(), 0
    name = "fictional-container"
    poll_hook = None

    def __init__(self, *_, **kwargs):
        self.audit = kwargs["audit_output"]
        self.calls = []
        self.monitor = SimpleNamespace(mark=lambda label: self.calls.append(label))
        self.last_submitted_ns = 100

    def stop_measurement(self):
        self.calls.append("stop")
        return resources()

    def close(self):
        self.calls.append("close")

    def finish(self):
        self.calls.append("finish")
        raw = b"fictional public native inventory"
        (self.audit / "native.json").write_bytes(raw)
        return {
            "status": "passed",
            "native_count": 1,
            "report_sha256": hashlib.sha256(raw).hexdigest(),
        }


@pytest.fixture
def context(tmp_path, monkeypatch):
    def audit(directory, label):
        path = directory / (label + "-native")
        path.mkdir()
        return path

    monkeypatch.setattr(run, "_audit_directory", audit)
    items = [case("ocr", n) for n in range(3)]
    sources = {item["id"]: {"id": item["id"]} for item in items}
    value = (tmp_path, tmp_path / "staging", sources, tmp_path / "assets", "2,4")
    return value, items


def test_episode_preserves_success_and_operational_failure_before_partial_stop(
    context, monkeypatch
):
    value, items = context
    sessions = []

    def create(*args, **kwargs):
        session = FakeSession(*args, **kwargs)

        def request(source, _):
            session.calls.append(source["id"])
            if source["id"] == items[1]["id"]:
                session.last_submitted_ns = 200
                raise DevelopmentError("candidate_timeout")
            return record(items[0])

        session.request = request
        sessions.append(session)
        return session

    monkeypatch.setattr(run, "ContainerSession", create)
    monkeypatch.setattr(run.time, "monotonic_ns", lambda: 60000000200)
    with pytest.raises(DevelopmentError) as caught:
        run.episode({"engine": "paddle"}, items, warm=[], repeat=3, label="partial", context=value)
    assert caught.value.reason == "candidate_timeout"
    report = json.loads((value[0] / "partial.json").read_text())
    assert report["status"] == "partial" and report["planned_case_ids"] == [
        item["id"] for item in items
    ]
    assert [row["case_id"] for row in report["records"]] == [item["id"] for item in items[:2]]
    failed = report["records"][1]
    assert failed["output"]["status"] == "failed" and failed["output"]["parsed"]["fields"] == []
    assert failed["submitted_ns"] == 200 and failed["returned_ns"] == 60000000200
    assert sessions[0].calls[-2:] == ["stop", "close"] and "finish" not in sessions[0].calls


def sdk_event(edge="start", sha=SHA):
    return {
        "phase": "sdk_recognize",
        "page_index": 0,
        "edge": edge,
        "at_ns": 10,
        "details": {"raster_rgb_sha256": sha, "width": 12, "height": 8}
        if edge == "start"
        else {"outcome": "passed"},
    }


def write_trace(path, events):
    path.write_bytes(b"".join(json.dumps(event).encode() + b"\n" for event in events))


@pytest.mark.parametrize(
    "fault,reason",
    [
        ("end_before", "controlled_timeout_too_late"),
        ("end_during_pause", "controlled_timeout_too_late"),
        ("sha", "candidate_raster_drift"),
        ("not_paused", "controlled_timeout_invalid"),
    ],
)
def test_controlled_pause_requires_real_open_matching_sdk_interval(
    tmp_path, monkeypatch, fault, reason
):
    path, commands = tmp_path / "phases.jsonl", []
    write_trace(
        path,
        [sdk_event(sha="b" * 64 if fault == "sha" else SHA)]
        + ([sdk_event("end")] if fault == "end_before" else []),
    )

    def command(args, **kwargs):
        commands.append(args)
        if args[1] == "pause" and fault == "end_during_pause":
            write_trace(path, [sdk_event(), sdk_event("end")])
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps({"Paused": fault != "not_paused", "Running": True}).encode(),
        )

    monkeypatch.setattr(run, "_command", command)
    hook = run.ControlledTimeout(path, SHA)
    with pytest.raises(DevelopmentError) as caught:
        hook(SimpleNamespace(name="fictional-container"))
    assert caught.value.reason == reason and hook.evidence is None
    if fault in ("sha", "end_before"):
        assert commands == []


@pytest.mark.parametrize(
    "wait_ns,unpause_failure", [(60000000000, False), (59999999999, False), (60000000000, True)]
)
def test_timeout_waits_sixty_seconds_and_always_unpauses_closes_and_saves(
    context, monkeypatch, wait_ns, unpause_failure
):
    value, items = context
    item, sessions, commands = items[0], [], []

    def create(*args, **kwargs):
        session = FakeSession(*args, **kwargs)

        def request(*_):
            write_trace(session.audit / "phases.jsonl", [sdk_event()])
            session.poll_hook(session)
            raise DevelopmentError("candidate_timeout")

        session.request = request
        sessions.append(session)
        return session

    def command(args, **kwargs):
        commands.append(args[1])
        return SimpleNamespace(
            returncode=int(args[1] == "unpause" and unpause_failure),
            stdout=b'{"Paused":true,"Running":true}',
        )

    monkeypatch.setattr(run, "ContainerSession", create)
    monkeypatch.setattr(run, "_command", command)
    monkeypatch.setattr(run.time, "monotonic_ns", lambda: wait_ns + 100)
    reason = (
        "controlled_timeout_wait_invalid"
        if wait_ns < 60000000000
        else "controlled_timeout_cleanup_failed"
        if unpause_failure
        else None
    )
    if reason:
        with pytest.raises(DevelopmentError) as caught:
            run.timeout_case({"engine": "paddle"}, item, label="timeout", context=value)
        assert caught.value.reason == reason
    else:
        report = run.timeout_case({"engine": "paddle"}, item, label="timeout", context=value)
        assert report["record"]["returned_ns"] - report["record"]["submitted_ns"] == wait_ns
        assert report["record"]["native_audit_complete"] is False
        assert report["record"]["incomplete_phase_events"] == [sdk_event()]
    assert "unpause" in commands and sessions[0].calls[-1] == "close"
    assert (
        commands.count("inspect") == 1
    )  # Known successful pause does not depend on another inspect.
    assert sessions[0].poll_hook is None and (value[0] / "timeout.json").exists()
    saved = json.loads((value[0] / "timeout.json").read_text())
    assert saved["record"]["returned_ns"] - saved["record"]["submitted_ns"] == wait_ns
    assert saved["record"]["controlled_fault"]["state"]["Paused"] is True


@pytest.mark.parametrize(
    "fault,reason",
    [
        ("processed", "benchmark_error_not_rejected"),
        ("render", "benchmark_error_allocated_or_ocr_called"),
        ("reason", "benchmark_error_reason_invalid"),
        ("lost_layer", "existing_garbled_layer_not_retained"),
    ],
)
def test_error_guard_does_not_accept_a_success_or_a_hidden_render(fault, reason):
    item = case("error", error_kind="garbled_layer" if fault == "lost_layer" else "encrypted")
    actual = record(item)
    if fault == "processed":
        actual["output"]["status"] = "processed"
    elif fault == "render":
        actual["output"]["timings_ns"]["events"] = [{"phase": "pdf_render"}]
    elif fault == "reason":
        actual["output"]["reason"] = "processing_failed"
    else:
        actual["output"]["pages"][0]["layer"] = "absent"
    with pytest.raises(DevelopmentError) as caught:
        run._error_guard(item, actual)
    assert caught.value.reason == reason


def test_partial_trace_tail_is_never_invented_as_a_finished_sdk_interval(tmp_path):
    path = tmp_path / "phases.jsonl"
    write_trace(path, [sdk_event()])
    with path.open("ab") as stream:
        stream.write(b'{"phase":"sdk_recognize","edge":"end"')
    assert run._trace(path) == [sdk_event()]


@pytest.mark.parametrize("status", ["manual", "failed"])
@pytest.mark.parametrize("fault", ["sha", "page"])
def test_failed_sdk_starts_still_require_the_original_raster_and_page(status, fault):
    item, event = case("ocr"), sdk_event(sha="b" * 64 if fault == "sha" else SHA)
    if fault == "page":
        event["page_index"] = 1
    actual = record(item)
    actual["output"].update(status=status, reason="engine_recognition_failed")
    actual["output"]["timings_ns"]["events"] = [event]
    with pytest.raises(DevelopmentError) as caught:
        run._raster_guard(item, actual)
    assert caught.value.reason == "candidate_raster_drift"


def test_resource_gate_requires_a_sample_before_go_and_ordered_initialization():
    session = FakeSession(audit_output=None)
    for fault in ("incomplete", "no_samples", "after_go", "initialization"):
        report = resources()
        actual = deepcopy(session)
        if fault == "incomplete":
            report["status"] = "failed"
        elif fault == "no_samples":
            report["samples"] = []
        elif fault == "after_go":
            report["samples"][0]["sample_end_ns"] = session.gate_sent_ns + 1
        else:
            actual.initialization_timings = {"start": 4, "end": 8}
        with pytest.raises(DevelopmentError) as caught:
            run._resource_guard(actual, report)
        assert caught.value.reason == "benchmark_measurement_invalid"


@pytest.mark.parametrize("engine", ["paddle", "tesseract"])
def test_missing_model_evidence_is_real_file_hash_and_has_no_invented_latency(tmp_path, engine):
    directory = tmp_path / "candidate-reports" / ("missing-models-" + engine)
    directory.mkdir(parents=True)
    path = directory / "report.json"
    negative = {
        "engine": engine,
        "status": "failed",
        "reason": "candidate_models_invalid",
        "initialization_attempted": False,
        "initialization": None,
        "inference_performed": False,
    }
    path.write_text(json.dumps(negative), encoding="utf8")
    result = run.missing_model_case(
        case("error", error_kind="missing_model"), engine, tmp_path / "assets"
    )
    assert result["submitted_ns"] is None and result["returned_ns"] is None
    assert result["evidence"]["sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
    assert result["output"]["status"] == "failed" and result["output"]["parsed"]["fields"] == []
    for key in ("initialization_attempted", "inference_performed"):
        negative[key] = True
        path.write_text(json.dumps(negative), encoding="utf8")
        with pytest.raises(DevelopmentError) as caught:
            run.missing_model_case(
                case("error", error_kind="missing_model"), engine, tmp_path / "assets"
            )
        assert caught.value.reason == "benchmark_missing_model_evidence_invalid"
        negative[key] = False


def preallocation_rejection():
    item = case("error", error_kind="pixel_limit")
    actual = record(item)
    actual["output"]["pages"][0].update(layer="absent", reason_code="prepare_limit")
    actual["output"]["timings_ns"]["events"] = [
        {"phase": "pdf_render", "page_index": 0, "edge": "start", "at_ns": 10, "details": None},
        {
            "phase": "pdf_render",
            "page_index": 0,
            "edge": "end",
            "at_ns": 20,
            "details": {"outcome": "failed", "bitmap_allocation_attempted": False},
        },
    ]
    return item, actual


def test_pixel_limit_allows_a_failed_render_call_only_with_proven_no_allocation():
    item, actual = preallocation_rejection()
    before = deepcopy(actual)
    run._error_guard(item, actual)
    assert actual == before


@pytest.mark.parametrize(
    "fault",
    [
        "allocation_true",
        "allocation_missing",
        "allocation_zero",
        "allocation_null",
        "allocation_string",
        "missing_end",
        "missing_start",
        "reversed_pair",
        "duplicate_pair",
        "different_page",
        "boolean_page",
        "negative_page",
        "missing_time",
        "boolean_time",
        "reversed_time",
        "end_passed",
        "details_missing",
        "nonpixel",
        "wrong_reason",
        "failed_status",
        "missing_page",
        "multiple_pages",
        "wrong_output_page",
        "boolean_output_page",
        "present_layer",
        "render_route",
        "wrong_page_reason",
        "raster_present",
    ],
)
def test_error_render_exception_requires_complete_matching_preallocation_evidence(fault):
    item, actual = preallocation_rejection()
    result = actual["output"]
    events, page = result["timings_ns"]["events"], result["pages"][0]
    if fault.startswith("allocation_"):
        details = events[1]["details"]
        if fault == "allocation_missing":
            del details["bitmap_allocation_attempted"]
        else:
            details["bitmap_allocation_attempted"] = {
                "allocation_true": True,
                "allocation_zero": 0,
                "allocation_null": None,
                "allocation_string": "false",
            }[fault]
    elif fault in ("missing_end", "missing_start"):
        events.pop(1 if fault == "missing_end" else 0)
    elif fault == "reversed_pair":
        events.reverse()
    elif fault == "duplicate_pair":
        events.extend(deepcopy(events))
    elif fault == "different_page":
        events[1]["page_index"] = 1
    elif fault in ("boolean_page", "negative_page"):
        for event in events:
            event["page_index"] = False if fault == "boolean_page" else -1
    elif fault == "missing_time":
        del events[1]["at_ns"]
    elif fault == "boolean_time":
        events[0]["at_ns"] = True
    elif fault == "reversed_time":
        events[1]["at_ns"] = 9
    elif fault == "end_passed":
        events[1]["details"]["outcome"] = "passed"
    elif fault == "details_missing":
        del events[1]["details"]
    elif fault == "nonpixel":
        item["error_kind"] = "stream_limit"
    elif fault == "wrong_reason":
        result["reason"] = "probe_limit"
    elif fault == "failed_status":
        result["status"] = "failed"
    elif fault == "missing_page":
        result["pages"] = []
    elif fault == "multiple_pages":
        result["pages"].append({**page, "page_index": 1})
    elif fault == "wrong_output_page":
        page["page_index"] = 1
    elif fault == "boolean_output_page":
        page["page_index"] = False
    elif fault == "present_layer":
        page["layer"] = "present"
    elif fault == "render_route":
        page["route"] = "render"
    elif fault == "wrong_page_reason":
        page["reason_code"] = "probe_limit"
    else:
        page["raster_rgb_sha256"] = SHA
    with pytest.raises(DevelopmentError) as caught:
        run._error_guard(item, actual)
    assert caught.value.reason == "benchmark_error_allocated_or_ocr_called"


@pytest.mark.parametrize("phase", ["sdk_recognize", "rgb_materialize"])
@pytest.mark.parametrize("edge", ["start", "end", None])
def test_no_sdk_or_rgb_event_is_allowed_even_with_a_proven_preallocation_rejection(phase, edge):
    item, actual = preallocation_rejection()
    actual["output"]["timings_ns"]["events"].append({"phase": phase, "edge": edge})
    with pytest.raises(DevelopmentError) as caught:
        run._error_guard(item, actual)
    assert caught.value.reason == "benchmark_error_allocated_or_ocr_called"
