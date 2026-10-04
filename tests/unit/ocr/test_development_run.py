"""DEV-only selection and bounded transport contracts, without running Docker or OCR."""

import hashlib
import io
import json
import subprocess
from copy import deepcopy
from fractions import Fraction
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts.ocr_benchmark import development_run as runner


def rejected(reason):
    return pytest.raises(runner.DevelopmentError, check=lambda error: error.reason == reason)


def cases():
    return [
        {
            "id": f"D{index:02d}-scan",
            "template_id": f"D{index:02d}",
            "group": "development",
            "carrier": "scan_pdf",
            "expected_fields": {"header.total": str(index)},
            "reference_text": "ab",
        }
        for index in range(1, 5)
    ]


def output(value="1", *, status="processed"):
    return {
        "status": status,
        "reason": None if status == "processed" else "manual_review",
        "parsed": {"fields": [{"path": "header.total", "value": value, "status": "certain"}]},
        "raw_text": "ab",
        "pages": [{"page_index": 0, "evidence": {"bbox": [1, 2, 3, 4]}}],
        "timings_ns": {"recognition": 123},
    }


def reports():
    return [
        {"profile": dict(profile), "C": 2, "T": 4, "E": 0, "edits": 1, "reference_chars": 4}
        for profile in runner.PROFILES
    ]


def report_for(values, model, psm):
    return next(
        report
        for report in values
        if report["profile"] == {"engine": "tesseract", "model_set": model, "psm": psm}
    )


def test_profiles_are_exactly_the_six_tesseract_choices_and_one_fixed_paddle():
    assert list(runner.PROFILES) == [
        {"engine": "tesseract", "model_set": model, "psm": psm}
        for model in ("fast", "best")
        for psm in (6, 3, 11)
    ] + [{"engine": "paddle"}]


def test_development_selection_sorts_only_four_dev_inputs_and_ignores_formal_cases():
    original = cases()
    manifest = {"development": original[::-1], "cases": [{"template_id": "S01"}]}
    before = deepcopy(manifest)
    assert runner.development_cases(manifest) == original
    assert manifest == before


@pytest.mark.parametrize("mutation", ["missing", "extra", "duplicate", "formal", "photo"])
def test_invalid_dev_input_sets_are_not_silently_filtered(mutation):
    dev = cases()
    if mutation == "missing":
        dev.pop()
    elif mutation == "extra":
        dev.append(deepcopy(dev[0]))
    elif mutation == "duplicate":
        dev[-1]["template_id"] = "D01"
    elif mutation == "formal":
        dev[0]["group"] = "ocr"
    else:
        dev[0]["carrier"] = "photo"
    with rejected("development_inputs_invalid"):
        runner.development_cases({"development": dev})


@pytest.mark.parametrize("status", ["manual", "failed"])
def test_terminal_nonprocessed_cases_keep_all_expected_fields_and_text_in_denominators(status):
    predictions = [output(str(index), status=status) for index in range(1, 5)]
    summary = runner.summarize_profile(
        runner.PROFILES[0], cases(), [predictions, deepcopy(predictions)]
    )
    assert (summary["C"], summary["T"], summary["E"]) == (0, 4, 0)
    assert (summary["edits"], summary["reference_chars"]) == (8, 8)
    assert len(summary["documents"]) == 4
    assert all(
        document["status"] == status and document["T"] == 1 for document in summary["documents"]
    )


def test_scoring_uses_one_pass_and_counts_extra_review_missing_and_failed():
    predictions = [output("1"), output("2"), output("wrong"), output("4", status="failed")]
    predictions[0]["parsed"]["fields"].append(
        {"path": "header.tax", "value": "0", "status": "certain"}
    )
    predictions[1]["parsed"]["fields"][0]["status"] = "review"
    predictions[2]["raw_text"] = "x"
    second = deepcopy(predictions)
    for value in second:
        value["timings_ns"] = {"recognition": 987}
    summary = runner.summarize_profile(runner.PROFILES[-1], cases(), [predictions, second])
    assert (summary["C"], summary["T"], summary["E"]) == (1, 4, 1)
    assert Fraction(summary["C"], summary["T"] + summary["E"]) == Fraction(1, 5)
    assert (summary["edits"], summary["reference_chars"]) == (4, 8)
    assert summary["stable_passes"] == 2


@pytest.mark.parametrize("changed", ["raw_text", "reason", "fields", "evidence"])
def test_second_pass_cannot_rescue_or_change_any_semantic_output(changed):
    first = [output(str(index)) for index in range(1, 5)]
    second = deepcopy(first)
    if changed == "fields":
        second[0]["parsed"]["fields"][0]["value"] = "wrong"
    elif changed == "evidence":
        second[0]["pages"][0]["evidence"]["bbox"][0] = 9
    else:
        second[0][changed] = "changed"
    with rejected("candidate_output_unstable"):
        runner.summarize_profile(runner.PROFILES[0], cases(), [first, second])


@pytest.mark.parametrize("passes", [[], [[]], [[], []], [[], [], []]])
def test_missing_or_extra_passes_cannot_shrink_the_dev_denominator(passes):
    with rejected("development_results_invalid"):
        runner.summarize_profile(runner.PROFILES[0], cases(), passes)


def test_selection_uses_exact_accuracy_including_extra_predictions_before_cer():
    values = reports()
    winner = report_for(values, "best", 11)
    winner.update(C=2, T=4, E=0, edits=100)
    for report in values:
        if report is not winner:
            report.update(C=3, T=4, E=3, edits=0)
    assert runner.select_tesseract(values) == winner["profile"]


def test_cer_tie_breaker_is_a_ratio_and_precedes_fast_preference():
    values = reports()
    winner = report_for(values, "best", 3)
    winner.update(edits=2, reference_chars=12)
    assert runner.select_tesseract(values) == winner["profile"]


def test_equal_accuracy_and_cer_prefer_fast_even_when_best_has_preferred_psm():
    values = reports()
    for report in values:
        if report["profile"]["engine"] == "tesseract" and report["profile"]["model_set"] == "fast":
            report.update(C=1, T=2, edits=2, reference_chars=8)
    assert runner.select_tesseract(values) == {"engine": "tesseract", "model_set": "fast", "psm": 6}


@pytest.mark.parametrize("preferred,remaining", [(6, [6, 3, 11]), (3, [3, 11]), (11, [11])])
def test_final_psm_order_is_fixed_not_report_iteration_order(preferred, remaining):
    values = reports()[::-1]
    for psm in remaining:
        report_for(values, "fast", psm).update(C=3)
    assert runner.select_tesseract(values)["psm"] == preferred


@pytest.mark.parametrize("change", ["missing", "duplicate", "unknown", "empty_truth"])
def test_selection_requires_all_six_valid_tesseract_reports(change):
    values = reports()
    if change == "missing":
        values.pop(0)
    elif change == "duplicate":
        values[0] = deepcopy(values[1])
    elif change == "unknown":
        values[0]["profile"]["psm"] = 1
    else:
        values[0].update(T=0, E=0)
    with rejected("development_results_invalid"):
        runner.select_tesseract(values)


@pytest.mark.parametrize(
    "data",
    [
        b"[]",
        b'{"v":true}',
        b'{"v":2}',
        b'{"v":1,"v":1}',
        b'{"v":1,"x":{"a":1,"a":2}}',
        b'{"v":1,"x":NaN}',
        b'{"v":1,"x":Infinity}',
        b'{"v":1,"x":"\xff"}',
        b"{",
    ],
)
def test_ndjson_decoder_rejects_noncanonical_or_invalid_protocol(data):
    with rejected("candidate_protocol_invalid"):
        runner._decode(data)


class Stream(io.BytesIO):
    def fileno(self):
        return id(self)


class Process:
    def __init__(self, *, lingering=False):
        self.stdin, self.stdout, self.stderr = Stream(), Stream(), Stream()
        self.lingering, self.killed, self.waits = lingering, False, []
        self.exit_code = 0

    def wait(self, *, timeout):
        self.waits.append(timeout)
        if self.lingering and not self.killed:
            raise subprocess.TimeoutExpired("fake candidate CLI", timeout)
        return self.exit_code

    def poll(self):
        return None if self.lingering and not self.killed else self.exit_code

    def kill(self):
        self.killed = True


class Selector:
    def __init__(self):
        self.closed, self.unregistered = False, []

    def select(self, timeout):
        return []

    def unregister(self, stream):
        self.unregistered.append(stream)

    def register(self, *args):
        pass

    def close(self):
        self.closed = True


def session(*, lingering=False):
    value = runner.ContainerSession.__new__(runner.ContainerSession)
    value.name, value.request_id = "coinpup-dev-fictional", 0
    value.audit_finished = False
    value.buffer, value.stderr, value.stderr_bytes = bytearray(), bytearray(), 0
    value.process, value.selector = Process(lingering=lingering), Selector()
    return value


def test_complete_json_line_cannot_bypass_stdout_limit_with_a_newline():
    value = session()
    value.buffer.extend(b'{"v":1,"x":"' + b"x" * runner.FRAME_BYTES + b'"}\n')
    with rejected("candidate_output_limit"):
        value._frame(100)


def test_exact_stdout_frame_byte_limit_remains_accepted():
    value = session()
    prefix, suffix = b'{"v":1,"x":"', b'"}'
    payload = prefix + b"x" * (runner.FRAME_BYTES - len(prefix) - len(suffix)) + suffix
    assert len(payload) == runner.FRAME_BYTES
    value.buffer.extend(payload + b"\n")
    assert len(value._frame(100)["x"]) == runner.FRAME_BYTES - len(prefix) - len(suffix)


def test_buffered_stdout_without_a_newline_is_also_bounded():
    value = session()
    value.buffer.extend(b"x" * (runner.FRAME_BYTES + 1))
    with rejected("candidate_output_limit"):
        value._frame(100)


def test_stderr_budget_is_cumulative_even_when_only_a_short_tail_is_retained(monkeypatch):
    value = session()
    value.stderr_bytes = runner.FRAME_BYTES
    value.stderr.extend(b"short tail")
    monkeypatch.setattr(runner.time, "monotonic", lambda: 0)
    monkeypatch.setattr(
        value.selector,
        "select",
        lambda timeout: [(SimpleNamespace(fileobj=value.process.stderr), 1)],
    )
    monkeypatch.setattr(runner.os, "read", lambda fd, size: b"x")
    with rejected("candidate_diagnostic_limit"):
        value._frame(60)


def test_request_deadline_renews_only_after_each_unique_page_done(monkeypatch):
    value, seen = session(), []
    times = iter([10, 25, 40])
    frames = iter(
        [
            {"v": 1, "id": 1, "event": "page_done", "page_index": 0},
            {"v": 1, "id": 1, "event": "page_done", "page_index": 1},
            {
                "v": 1,
                "id": 1,
                "event": "result",
                "result": {"pages": [{"page_index": 0}, {"page_index": 1}]},
            },
        ]
    )
    monkeypatch.setattr(runner.time, "monotonic", lambda: next(times))
    monkeypatch.setattr(value, "_frame", lambda deadline: seen.append(deadline) or next(frames))
    result = value.request({"path": "/opt/staging/fictional.pdf"}, "application/pdf")
    assert seen == [70, 85, 100]
    assert result["output"]["pages"] == [{"page_index": 0}, {"page_index": 1}]
    assert result["returned_ns"] >= result["submitted_ns"]
    assert json.loads(value.process.stdin.getvalue())["id"] == 1


@pytest.mark.parametrize(
    "second",
    [
        {"v": 1, "id": 1, "event": "page_done", "page_index": 0},
        {"v": 1, "id": 1, "event": "page_done", "page_index": True},
        {"v": 1, "id": 2, "event": "result", "result": {"pages": []}},
        {"v": 1, "id": 1, "event": "result", "result": {"pages": []}},
        {"v": 1, "id": 1, "event": "result", "result": {"pages": [{"page_index": False}]}},
        {
            "v": 1,
            "id": 1,
            "event": "result",
            "result": {"pages": [{"page_index": 0}, {"page_index": 0}]},
        },
        {"v": 1, "id": 1, "event": "heartbeat"},
    ],
)
def test_bad_progress_or_result_does_not_extend_or_complete_a_request(monkeypatch, second):
    value = session()
    frames = iter([{"v": 1, "id": 1, "event": "page_done", "page_index": 0}, second])
    monkeypatch.setattr(value, "_frame", lambda deadline: next(frames))
    with rejected("candidate_protocol_invalid"):
        value.request({"path": "/opt/staging/fictional.pdf"}, "application/pdf")


def test_frame_deadline_expires_without_waiting_or_refreshing_on_no_progress(monkeypatch):
    value = session()
    monkeypatch.setattr(runner.time, "monotonic", lambda: 61)
    monkeypatch.setattr(
        value.selector, "select", lambda timeout: pytest.fail("Expired deadline must not block.")
    )
    with rejected("candidate_timeout"):
        value._frame(60)


def test_constructor_uses_a_120_second_startup_deadline_and_fixed_isolation(monkeypatch):
    process, selector, deadlines, commands = Process(), Selector(), [], []
    profile = runner.PROFILES[0]
    monkeypatch.setattr(runner.sys, "platform", "linux")
    monkeypatch.setattr(runner.time, "monotonic", lambda: 7)
    monkeypatch.setattr(runner.selectors, "DefaultSelector", lambda: selector)
    monkeypatch.setattr(
        runner.subprocess,
        "Popen",
        lambda args, **kwargs: commands.append((args, kwargs)) or process,
    )
    monkeypatch.setattr(
        runner.ContainerSession,
        "_frame",
        lambda self, deadline: (
            deadlines.append(deadline)
            or {"v": 1, "event": "ready", "metadata": {"profile": profile}}
        ),
    )
    value = runner.ContainerSession(
        profile, Path("/fictional/staging"), Path("/fictional/assets"), "2,4"
    )
    assert deadlines == [127]
    command, kwargs = commands[0]
    assert command[command.index("--network") + 1] == "none"
    assert command[command.index("--cpuset-cpus") + 1] == "2,4"
    assert command[command.index("--memory") + 1] == "4g"
    assert "--read-only" in command and kwargs["start_new_session"] is True
    assert value.metadata == {"profile": profile}


@pytest.mark.parametrize("metadata", [{}, {"profile": {"engine": "paddle"}}])
def test_startup_metadata_must_identify_the_same_requested_profile(monkeypatch, metadata):
    closed = []
    monkeypatch.setattr(runner.sys, "platform", "linux")
    monkeypatch.setattr(runner.selectors, "DefaultSelector", Selector)
    monkeypatch.setattr(runner.subprocess, "Popen", lambda *args, **kwargs: Process())
    monkeypatch.setattr(
        runner.ContainerSession,
        "_frame",
        lambda self, deadline: {"v": 1, "event": "ready", "metadata": metadata},
    )
    monkeypatch.setattr(runner.ContainerSession, "close", lambda self: closed.append(True))
    with rejected("candidate_startup_failed"):
        runner.ContainerSession(
            runner.PROFILES[0], Path("/fictional/staging"), Path("/fictional/assets"), "2,4"
        )
    assert closed == [True]


def test_finish_has_a_separate_120_second_deadline_and_strict_next_request_id(monkeypatch):
    value, deadlines = session(), []
    value.request_id = 8
    summary = {"status": "passed", "reason": None, "native_count": 12, "report_sha256": "a" * 64}
    monkeypatch.setattr(runner.time, "monotonic", lambda: 9)
    monkeypatch.setattr(
        value,
        "_frame",
        lambda deadline: (
            deadlines.append(deadline)
            or {"v": 1, "id": 9, "event": "audit_done", "summary": summary}
        ),
    )
    assert value.finish() == summary
    assert deadlines == [129]
    assert json.loads(value.process.stdin.getvalue()) == {"v": 1, "id": 9, "action": "finish"}


@pytest.mark.parametrize("defect", ["id", "boolean_id", "failed", "zero", "boolean_count", "event"])
def test_invalid_native_audit_terminal_never_passes_finish(monkeypatch, defect):
    value = session()
    frame = {
        "v": 1,
        "id": 1,
        "event": "audit_done",
        "summary": {"status": "passed", "native_count": 12},
    }
    if defect == "id":
        frame["id"] = 2
    elif defect == "boolean_id":
        frame["id"] = True
    elif defect == "event":
        frame["event"] = "audit_failed"
    else:
        frame["summary"]["status" if defect == "failed" else "native_count"] = (
            "failed" if defect == "failed" else (True if defect == "boolean_count" else 0)
        )
    monkeypatch.setattr(value, "_frame", lambda deadline: frame)
    with rejected("candidate_recognition_audit_failed"):
        value.finish()


def test_successful_audit_does_not_authorize_nonzero_worker_exit(monkeypatch):
    value = session()
    process, selector = value.process, value.selector
    monkeypatch.setattr(
        value,
        "_frame",
        lambda deadline: {
            "v": 1,
            "id": 1,
            "event": "audit_done",
            "summary": {"status": "passed", "native_count": 1, "report_sha256": "a" * 64},
        },
    )
    value.finish()
    assert value.audit_finished is True
    process.exit_code = 7
    monkeypatch.setattr(
        runner,
        "_command",
        lambda args, **kwargs: SimpleNamespace(
            returncode=1 if args[1] == "inspect" else 0, stdout=b"[]"
        ),
    )
    with rejected("candidate_process_failed"):
        value.close()
    assert process.stdout.closed and process.stderr.closed and selector.closed


@pytest.mark.parametrize("confirmed", [True, False])
def test_cleanup_always_closes_local_cli_and_pipes_and_requires_docker_confirmation(
    monkeypatch, confirmed
):
    value = session(lingering=True)
    process, selector, commands = value.process, value.selector, []

    def control(args, *, timeout):
        commands.append((args, timeout))
        return SimpleNamespace(
            returncode=1 if args[1] == "inspect" and confirmed else 0,
            stdout=b"[]" if confirmed else b"[{}]",
        )

    monkeypatch.setattr(runner, "_command", control)
    if confirmed:
        value.close()
    else:
        with rejected("candidate_cleanup_unconfirmed"):
            value.close()
    assert process.killed and process.waits == [20, 20]
    assert process.stdin.closed and process.stdout.closed and process.stderr.closed
    assert selector.closed
    assert [(args[1], timeout) for args, timeout in commands] == [("rm", 20), ("inspect", 20)]


@pytest.fixture
def dev_run(tmp_path, monkeypatch):
    corpus, output_dir = tmp_path / "corpus", tmp_path / "output"
    corpus.mkdir()
    dev = cases()
    for index, case in enumerate(dev):
        raw = b"fictional DEV bytes " + bytes([index])
        case.update(
            relative_path=f"dev-{index}.pdf",
            media_type="application/pdf",
            sha256=hashlib.sha256(raw).hexdigest(),
            byte_size=len(raw),
            raster_sha256=[f"raster-{index}"],
        )
        (corpus / case["relative_path"]).write_bytes(raw)
    manifest = {"development": dev[::-1], "cases": [{"relative_path": "must-never-be-opened.pdf"}]}
    (corpus / "manifest.json").write_text(json.dumps(manifest))
    fixtures = tmp_path / "tests/fixtures/ocr"
    fixtures.mkdir(parents=True)
    (fixtures / "corpus-frozen.json").write_text(
        json.dumps(
            {"manifest_sha256": hashlib.sha256((corpus / "manifest.json").read_bytes()).hexdigest()}
        )
    )
    calls, closed, faults = [], [], [None]

    class Candidate:
        def __init__(self, profile, staging, assets, cpus, *, audit_output):
            self.profile, self.staging = deepcopy(profile), staging
            self.audit_output = audit_output
            self.stderr_bytes, self.stderr = 0, bytearray()
            self.metadata, self.started_ns, self.ready_ns = {"profile": profile}, 10, 20

        def request(self, source, media_type):
            calls.append((self.profile, source, media_type))
            index = len(calls) % 4 or 4
            value = output(str(index))
            value["pages"] = [{"page_index": 0, "raster_rgb_sha256": f"raster-{index - 1}"}]
            return {"output": value, "submitted_ns": 30, "returned_ns": 40}

        def finish(self):
            if faults[0] == "failed":
                raise runner.DevelopmentError("candidate_recognition_audit_failed")
            raw = b'{"fictional_native_audit":true}'
            (self.audit_output / "native.json").write_bytes(raw)
            return {
                "status": "passed",
                "native_count": 1,
                "report_sha256": "0" * 64
                if faults[0] == "hash"
                else hashlib.sha256(raw).hexdigest(),
            }

        def close(self):
            closed.append(self.profile)
            if faults[0] == "exit":
                raise runner.DevelopmentError("candidate_process_failed")

    monkeypatch.setattr(runner, "ROOT", tmp_path)
    monkeypatch.setattr(runner.sys, "platform", "linux")
    monkeypatch.setattr(runner.os, "sched_getaffinity", lambda pid: {2, 4, 6}, raising=False)
    monkeypatch.setattr(runner, "verify_corpus", lambda directory: manifest)
    monkeypatch.setattr(runner, "_command", lambda args, **kwargs: SimpleNamespace(returncode=0))
    monkeypatch.setattr(runner, "ContainerSession", Candidate)
    return corpus, output_dir, tmp_path / "assets", dev, calls, closed, faults


def test_dev_orchestration_never_reads_or_submits_formal_carriers(dev_run):
    corpus, output_dir, assets, dev, calls, closed, _ = dev_run
    selection = runner.run_development(corpus, assets, output_dir)
    assert len(calls) == 7 * 2 * 4 and len(closed) == 7
    assert all(
        source["byte_size"] == dev[index % 4]["byte_size"]
        for index, (_, source, _) in enumerate(calls)
    )
    staging = output_dir.with_name(output_dir.name + "-staging")
    assert len(list(staging.iterdir())) == 4
    assert staging.parent == output_dir.parent and not staging.is_relative_to(output_dir)
    assert all(path.suffix == ".json" for path in output_dir.rglob("*") if path.is_file())
    if runner.os.name != "nt":
        assert staging.stat().st_mode & 0o777 == 0o700
        assert all(path.stat().st_mode & 0o777 == 0o600 for path in staging.iterdir())
    assert all(
        "D0" not in source["path"] and "dev-" not in source["path"] for _, source, _ in calls
    )
    assert selection["formal_inference_performed"] is False
    assert selection["tesseract"] == {"engine": "tesseract", "model_set": "fast", "psm": 6}


def test_existing_private_staging_is_never_reused_or_modified(dev_run):
    corpus, output_dir, assets, _, calls, closed, _ = dev_run
    staging = output_dir.with_name(output_dir.name + "-staging")
    staging.mkdir(mode=0o700)
    sentinel = staging / "preserved.txt"
    sentinel.write_bytes(b"fictional preserved source")
    with pytest.raises(FileExistsError):
        runner.run_development(corpus, assets, output_dir)
    assert sentinel.read_bytes() == b"fictional preserved source"
    assert list(staging.iterdir()) == [sentinel]
    assert calls == [] and closed == []


@pytest.mark.parametrize("fault", ["failed", "hash", "exit"])
def test_failed_or_mismatched_postrecognition_audit_blocks_selection_and_closes_session(
    dev_run, fault
):
    corpus, output_dir, assets, _, calls, closed, faults = dev_run
    faults[0] = fault
    with rejected(
        "candidate_process_failed" if fault == "exit" else "candidate_recognition_audit_failed"
    ):
        runner.run_development(corpus, assets, output_dir)
    assert len(calls) == 8 and len(closed) == 1
    assert not (output_dir / "selection.json").exists()
