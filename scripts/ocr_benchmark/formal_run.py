"""Execute one frozen, offline, same-host experiment; never tune from its results."""

import argparse
import hashlib
import json
import os
import sys
import time
import uuid
from pathlib import Path

from coinpup_api.ocr.recognize import failed_result

from .corpus import verify_corpus
from .development_run import ROOT, ContainerSession, DevelopmentError, _command, _fail, _save
from .formal_freeze import host_facts, load_and_verify, load_clear
from .formal_report import summarize_candidate
from .scoring import FieldScore, select_engine


def _json(path, maximum=1048576):
    with path.open("rb") as stream:
        data = stream.read(maximum + 1)
    if len(data) > maximum:
        _fail("benchmark_evidence_limit")
    return json.loads(data)


def _trace(path):
    if not path.exists():
        return []
    with path.open("rb") as stream:
        data = stream.read(262145)
    if len(data) > 262144:
        _fail("benchmark_trace_limit")
    lines = data.split(b"\n")[:-1]  # An append in progress is never treated as a complete event.
    if len(lines) > 500:
        _fail("benchmark_trace_limit")
    result = [json.loads(line) for line in lines]
    if any(type(row) is not dict for row in result):
        _fail("benchmark_trace_invalid")
    return result


def _stage(corpus_dir, cases, output_dir):
    staging = output_dir.with_name(output_dir.name + "-staging")
    staging.mkdir(mode=0o700)
    sources = {}
    for case in cases:
        raw = (corpus_dir / case["relative_path"]).read_bytes()
        if len(raw) != case["byte_size"] or hashlib.sha256(raw).hexdigest() != case["sha256"]:
            _fail("frozen_corpus_drift")
        name = uuid.uuid4().hex + (".pdf" if case["media_type"] == "application/pdf" else ".jpg")
        path = staging / name
        with path.open("xb") as stream:
            stream.write(raw)
        path.chmod(0o600)
        sources[case["id"]] = {
            "path": "/opt/staging/" + name,
            "sha256": case["sha256"],
            "byte_size": case["byte_size"],
        }
    if _command(["sudo", "chown", "-R", "10001:10001", str(staging)]).returncode:
        _fail("candidate_staging_invalid")
    return staging.resolve(), sources


def _audit_directory(output_dir, label):
    directory = output_dir / (label + "-native")
    directory.mkdir(mode=0o755)
    if _command(["sudo", "chown", "10001:10001", str(directory)]).returncode:
        _fail("candidate_staging_invalid")
    return directory.resolve()


def _raster_guard(case, record):
    output = record["output"]
    for event in output["timings_ns"].get("events", []):
        if event["phase"] == "sdk_recognize" and event["edge"] == "start":
            index = event["page_index"]
            if (
                type(index) is not int
                or not 0 <= index < len(case["raster_sha256"])
                or event["details"]["raster_rgb_sha256"] != case["raster_sha256"][index]
            ):
                _fail("candidate_raster_drift")
    if output["status"] == "processed" and case["group"] in ("ocr", "development", "degraded"):
        if [page["raster_rgb_sha256"] for page in output["pages"]] != case["raster_sha256"]:
            _fail("candidate_raster_drift")
    if case["group"] == "text":
        if any(page["raster_rgb_sha256"] is not None for page in output["pages"]):
            _fail("existing_layer_rendered")
        if any(
            event["phase"] in ("sdk_recognize", "pdf_render")
            for event in output["timings_ns"].get("events", [])
        ):
            _fail("existing_layer_ocr_called")


def _resource_guard(session, resources):
    if (
        resources["status"] != "complete"
        or not resources["samples"]
        or resources["samples"][0]["sample_end_ns"] > session.gate_sent_ns
        or not session.gate_sent_ns
        <= session.initialization_timings["start"]
        <= session.initialization_timings["end"]
        <= session.ready_ns
    ):
        _fail("benchmark_measurement_invalid")


def episode(profile, cases, *, warm, repeat, label, context):
    """One fresh process, fixed warming, raw requests and complete post-inference audit."""
    output_dir, staging, sources, assets, cpus = context
    audit_dir = _audit_directory(output_dir, label)
    session = ContainerSession(
        profile,
        staging,
        assets,
        cpus,
        audit_output=audit_dir,
        resource_output=output_dir / (label + "-resources.json"),
        phase_clock=True,
    )
    records, warmed = [], []
    report = {
        "label": label,
        "profile": profile,
        "started_ns": session.started_ns,
        "ready_ns": session.ready_ns,
        "gate_sent_ns": session.gate_sent_ns,
        "initialization_timings_ns": session.initialization_timings,
        "metadata": session.metadata,
        "warm_records": warmed,
        "records": records,
        "planned_case_ids": [case["id"] for case in cases],
        "status": "running",
    }
    try:
        for case in [*warm, *cases]:
            session.monitor.mark("request_before")
            try:
                record = session.request(sources[case["id"]], case["media_type"])
            except DevelopmentError as error:
                # An operational crash is not a measured successful response. Preserve
                # its real interval and full denominator, then invalidate the experiment.
                record = {
                    "submitted_ns": session.last_submitted_ns,
                    "returned_ns": time.monotonic_ns(),
                    "output": failed_result(error.reason),
                    "operational_failure": True,
                    "native_audit_complete": False,
                    "incomplete_phase_events": _trace(audit_dir / "phases.jsonl"),
                }
                record.update(case_id=case["id"], round=repeat)
                (warmed if case in warm else records).append(record)
                raise
            session.monitor.mark("request_after")
            record.update(case_id=case["id"], round=repeat)
            _raster_guard(case, record)
            (warmed if case in warm else records).append(record)
        resources = session.stop_measurement()
        _resource_guard(session, resources)
        report["resources"] = resources
        audit = session.finish()
        if (
            hashlib.sha256((audit_dir / "native.json").read_bytes()).hexdigest()
            != audit["report_sha256"]
        ):
            _fail("candidate_recognition_audit_failed")
        report["recognition_native_audit"] = audit
        report["diagnostics"] = {
            "stderr_bytes": session.stderr_bytes,
            "tail": bytes(session.stderr).decode("utf8", errors="replace"),
        }
        report["status"] = "complete"
    except BaseException as error:
        report.update(status="partial", reason=getattr(error, "reason", "formal_benchmark_failed"))
        raise
    finally:
        try:
            if "resources" not in report:
                report["resources"] = session.stop_measurement()
        finally:
            try:
                session.close()
            finally:
                _save(output_dir / (label + ".json"), report)
    return report


class ControlledTimeout:
    """Pause a genuine in-progress SDK call; preserve the unclosed interval and watchdog."""

    def __init__(self, journal, expected_raster):
        self.journal, self.expected_raster = journal, expected_raster
        self.evidence = None
        self.paused = False

    def __call__(self, session):
        if self.evidence is not None:
            return
        events = _trace(self.journal)
        starts = [
            row for row in events if row["phase"] == "sdk_recognize" and row["edge"] == "start"
        ]
        if not starts:
            return
        start = starts[-1]
        if start["details"]["raster_rgb_sha256"] != self.expected_raster:
            _fail("candidate_raster_drift")
        ends = [row for row in events if row["phase"] == "sdk_recognize" and row["edge"] == "end"]
        if ends:
            _fail("controlled_timeout_too_late")
        requested = time.monotonic_ns()
        paused = _command(["docker", "pause", session.name], timeout=10)
        returned = time.monotonic_ns()
        self.paused = paused.returncode == 0
        state = _command(["docker", "inspect", "--format", "{{json .State}}", session.name])
        if paused.returncode or state.returncode or _trace(self.journal) != events:
            # Other phase-end events may still have arrived; the SDK must remain open.
            if paused.returncode or state.returncode:
                _fail("controlled_timeout_invalid")
            current = _trace(self.journal)
            if any(row["phase"] == "sdk_recognize" and row["edge"] == "end" for row in current):
                _fail("controlled_timeout_too_late")
        actual = json.loads(state.stdout)
        if actual.get("Paused") is not True or actual.get("Running") is not True:
            _fail("controlled_timeout_invalid")
        self.evidence = {
            "mechanism": "actual_docker_cgroup_pause_during_sdk",
            "pause_submitted_ns": requested,
            "pause_returned_ns": returned,
            "sdk_start": start,
            "state": actual,
            "not_a_natural_model_timeout": True,
        }


def timeout_case(profile, case, *, label, context):
    output_dir, staging, sources, assets, cpus = context
    audit_dir = _audit_directory(output_dir, label)
    session = ContainerSession(
        profile,
        staging,
        assets,
        cpus,
        audit_output=audit_dir,
        resource_output=output_dir / (label + "-resources.json"),
        phase_clock=True,
    )
    fault = ControlledTimeout(audit_dir / "phases.jsonl", case["raster_sha256"][0])
    session.poll_hook = fault
    report = {"label": label, "profile": profile, "native_audit_complete": False}
    try:
        try:
            session.request(sources[case["id"]], case["media_type"])
        except DevelopmentError as error:
            returned = time.monotonic_ns()
            if error.reason != "candidate_timeout" or fault.evidence is None:
                raise
            record = {
                "case_id": case["id"],
                "round": 0,
                "submitted_ns": session.last_submitted_ns,
                "returned_ns": returned,
                "output": failed_result("candidate_timeout"),
                "controlled_fault": fault.evidence,
                "native_audit_complete": False,
                "incomplete_phase_events": _trace(audit_dir / "phases.jsonl"),
            }
            report["record"] = record
            if returned - session.last_submitted_ns < 60000000000:
                _fail("controlled_timeout_wait_invalid")
        else:
            _fail("controlled_timeout_not_observed")
        report["resources"] = session.stop_measurement()
        _resource_guard(session, report["resources"])
    finally:
        session.poll_hook = None
        try:
            if "resources" not in report:
                report["resources"] = session.stop_measurement()
        finally:
            try:
                paused = fault.paused
                if not paused:
                    state = _command(
                        ["docker", "inspect", "--format", "{{json .State}}", session.name]
                    )
                    paused = (
                        state.returncode == 0 and json.loads(state.stdout).get("Paused") is True
                    )
                if paused:
                    if _command(["docker", "unpause", session.name], timeout=10).returncode:
                        _fail("controlled_timeout_cleanup_failed")
            finally:
                try:
                    session.close()
                finally:
                    _save(output_dir / (label + ".json"), report)
    return report


def _error_guard(case, record):
    kind = case["error_kind"]
    output = record["output"]
    if output["status"] not in ("manual", "failed"):
        _fail("benchmark_error_not_rejected")
    events = output["timings_ns"].get("events", [])
    if any(event["phase"] in ("sdk_recognize", "rgb_materialize") for event in events):
        _fail("benchmark_error_allocated_or_ocr_called")
    renders = [event for event in events if event["phase"] == "pdf_render"]
    if renders:
        pages = output.get("pages", [])
        # PDFium calls the budget-checking maker inside page.render. Entering that
        # call is permitted only with explicit proof that no allocation was attempted.
        if not (
            kind == "pixel_limit"
            and output["status"] == "manual"
            and output["reason"] == "prepare_limit"
            and len(renders) == 2
            and [event.get("edge") for event in renders] == ["start", "end"]
            and all(type(event.get("page_index")) is int for event in renders)
            and 0 <= renders[0]["page_index"] < 50
            and renders[0]["page_index"] == renders[1]["page_index"]
            and all(type(event.get("at_ns")) is int for event in renders)
            and 0 <= renders[0]["at_ns"] <= renders[1]["at_ns"]
            and type(renders[1].get("details")) is dict
            and renders[1]["details"].get("outcome") == "failed"
            and renders[1]["details"].get("bitmap_allocation_attempted") is False
            and type(pages) is list
            and len(pages) == 1
            and type(pages[0]) is dict
            and type(pages[0].get("page_index")) is int
            and pages[0]["page_index"] == renders[0]["page_index"]
            and pages[0].get("layer") == "absent"
            and pages[0].get("route") == "manual"
            and pages[0].get("reason_code") == "prepare_limit"
            and pages[0].get("raster_rgb_sha256") is None
        ):
            _fail("benchmark_error_allocated_or_ocr_called")
    expected = {
        "encrypted": {"encrypted_pdf"},
        "page_limit": {"probe_limit"},
        "pixel_limit": {"probe_limit", "prepare_limit"},
        "stream_limit": {"probe_limit"},
    }
    if kind in expected and output["reason"] not in expected[kind]:
        _fail("benchmark_error_reason_invalid")
    if kind == "garbled_layer" and not any(
        page["layer"] == "present" and page["route"] == "manual" for page in output["pages"]
    ):
        _fail("existing_garbled_layer_not_retained")


def missing_model_case(case, engine, assets_dir):
    # These two real negative containers run earlier in this same CI job/host/images.
    path = assets_dir.parent / "candidate-reports" / ("missing-models-" + engine) / "report.json"
    data = _json(path, maximum=67108864)
    if (
        data.get("engine") != engine
        or data.get("status") != "failed"
        or data.get("reason") != "candidate_models_invalid"
        or data.get("initialization_attempted") is not False
        or data.get("initialization") is not None
        or data.get("inference_performed") is not False
    ):
        _fail("benchmark_missing_model_evidence_invalid")
    return {
        "case_id": case["id"],
        "round": 0,
        "submitted_ns": None,
        "returned_ns": None,
        "latency_status": "not_measured_for_earlier_same_job_preflight",
        "output": failed_result("candidate_models_invalid"),
        "evidence": {
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "scope": "same_job_actual_preflight_missing_models_before_initialization",
            "initialization_attempted": False,
            "inference_performed": False,
        },
    }


def _score(group):
    return FieldScore(group["C"], group["T"], group["E"])


def _compact(report):
    return {
        "groups": {
            name: {key: value for key, value in group.items() if key != "documents"}
            for name, group in report["groups"].items()
        },
        "stability": report["stability"],
        "hot_latencies": report["hot_latencies"],
        "cold": report["cold"],
        "resources": {
            "episodes": len(report["resources"]),
            "sampled_same_window_rss_peak_bytes": max(
                resource["same_window_rss_peak_bytes"] for resource in report["resources"]
            ),
            "cgroup_lifetime_memory_peak_bytes": max(
                resource["final"]["memory_peak_bytes"] for resource in report["resources"]
            ),
            "cpu_usage_delta_usec": sum(
                resource["cpu_usage_delta_usec"] for resource in report["resources"]
            ),
            "samples": sum(len(resource["samples"]) for resource in report["resources"]),
            "max_sample_gap_ns": max(
                (gap for resource in report["resources"] for gap in resource["sample_gaps_ns"]),
                default=None,
            ),
            "scope": "all_measured_initialization_warm_and_requests_excludes_native_audit_cleanup",
            "individual_hwm_in_full_artifacts_only_no_sum": True,
        },
    }


def run_formal(corpus_dir, assets_dir, output_dir, selection_path, *, clear_frozen=None):
    if sys.platform != "linux":
        _fail("candidate_platform_invalid")
    clear = clear_frozen is not None
    config = (
        load_clear(ROOT, corpus_dir, assets_dir, selection_path, clear_frozen)
        if clear
        else load_and_verify(ROOT, corpus_dir, assets_dir, selection_path)
    )
    repeats, version = (3, 2) if clear else (5, 1)
    manifest = verify_corpus(corpus_dir)
    cases = sorted(manifest["cases"], key=lambda case: case["id"])
    development = sorted(manifest["development"], key=lambda case: case["template_id"])
    groups = {
        name: [case for case in cases if case["group"] == name]
        for name in ("text", "ocr", "degraded", "error")
    }
    if [len(groups[name]) for name in groups] != ([12, 24, 0, 0] if clear else [12, 24, 12, 8]):
        _fail("frozen_corpus_shape_invalid")
    if [case["template_id"] for case in development] != ["D01", "D02", "D03", "D04"]:
        _fail("development_inputs_invalid")
    output_dir.mkdir(mode=0o700, parents=True, exist_ok=False)
    staging, sources = _stage(corpus_dir, [*cases, *development], output_dir)
    cpus = sorted(os.sched_getaffinity(0))[:2]
    if len(cpus) != 2:
        _fail("candidate_resource_invalid")
    context = output_dir, staging, sources, assets_dir.resolve(), ",".join(map(str, cpus))
    records = {engine: [] for engine in ("tesseract", "paddle")}
    cold, resources = ({engine: [] for engine in records} for _ in range(2))
    facts = host_facts()
    experiment = {
        "version": version,
        "status": "running",
        "host": facts,
        "config": config,
        "planned_case_ids": {
            name: [case["id"] for case in group] for name, group in groups.items()
        },
        "records": records,
        "cold": cold,
        "resources": resources,
        "started_ns": time.monotonic_ns(),
        "planned_documents": [
            {"case_id": case["id"], "group": case["group"], "T": len(case["expected_fields"])}
            for case in cases
        ],
    }
    _save(output_dir / "experiment.json", experiment)
    try:
        # Text is checked first; an existing layer never gains an OCR rescue path.
        for engine in records:
            report = episode(
                config["profiles"][engine],
                groups["text"],
                warm=[],
                repeat=0,
                label="text-" + engine,
                context=context,
            )
            records[engine].extend(report["records"])
            resources[engine].append(report["resources"])
            text_summary = summarize_candidate(groups["text"], report["records"])
            text = _score(text_summary["groups"]["text"])
            if text.correct != text.expected or text.extra != 0:
                _fail("text_gate_failed")
            print(
                "OCR_FORMAL_TEXT_V1="
                + json.dumps(
                    {"engine": engine, "C": text.correct, "T": text.expected, "E": text.extra}
                ),
                flush=True,
            )
        for round_number in range(repeats):
            order = ("tesseract", "paddle") if round_number % 2 == 0 else ("paddle", "tesseract")
            for engine in order:
                report = episode(
                    config["profiles"][engine],
                    [development[0]],
                    warm=[],
                    repeat=0,
                    label=f"cold-{round_number}-{engine}",
                    context=context,
                )
                cold[engine].append(
                    {
                        "started_ns": report["started_ns"],
                        "ready_ns": report["ready_ns"],
                        "initialization_timings_ns": report["initialization_timings_ns"],
                        "record": report["records"][0],
                    }
                )
                resources[engine].append(report["resources"])
        # Each hot round is a fresh process, using only fixed D01/D02 to prewarm.
        for round_number in range(repeats):
            order = ("tesseract", "paddle") if round_number % 2 == 0 else ("paddle", "tesseract")
            for engine in order:
                report = episode(
                    config["profiles"][engine],
                    groups["ocr"],
                    warm=development[:2],
                    repeat=round_number,
                    label=f"hot-{round_number}-{engine}",
                    context=context,
                )
                records[engine].extend(report["records"])
                resources[engine].append(report["resources"])
                print(
                    "OCR_FORMAL_ROUND_V1="
                    + json.dumps(
                        {
                            "engine": engine,
                            "round": round_number,
                            "terminal_documents": len(report["records"]),
                        }
                    ),
                    flush=True,
                )
        ordinary_errors = [case for case in groups["error"] if not case.get("runtime_scenario")]
        for engine in records:
            if clear:
                break  # Auxiliary groups remain covered by tests; no new expensive measurement.
            report = episode(
                config["profiles"][engine],
                [*groups["degraded"], *ordinary_errors],
                warm=development[:2],
                repeat=0,
                label="aux-" + engine,
                context=context,
            )
            for case, record in zip(
                [*groups["degraded"], *ordinary_errors], report["records"], strict=True
            ):
                if case["group"] == "error":
                    _error_guard(case, record)
            records[engine].extend(report["records"])
            resources[engine].append(report["resources"])
            for case in groups["error"]:
                scenario = (case.get("runtime_scenario") or {}).get("kind")
                if scenario == "missing_model":
                    records[engine].append(missing_model_case(case, engine, assets_dir))
                elif scenario == "timeout":
                    timed = timeout_case(
                        config["profiles"][engine], case, label="timeout-" + engine, context=context
                    )
                    records[engine].append(timed["record"])
                    resources[engine].append(timed["resources"])
        summaries = {
            engine: summarize_candidate(
                cases,
                records[engine],
                cold_records=cold[engine],
                resource_reports=resources[engine],
                hot_repeats=repeats,
            )
            for engine in records
        }
        experiment["candidates"] = summaries
        _save(output_dir / "experiment.json", experiment)
        if any(not report["stability"]["stable"] for report in summaries.values()):
            _fail("candidate_output_unstable")
        text_scores = [_score(summaries[engine]["groups"]["text"]) for engine in records]
        if text_scores[0] != text_scores[1]:
            _fail("text_candidate_difference")
        selected = select_engine(
            text_scores[0],
            _score(summaries["paddle"]["groups"]["ocr"]),
            _score(summaries["tesseract"]["groups"]["ocr"]),
        )
        decision = {"selected": selected.selected, "stop": selected.stop, "reason": selected.reason}
        if clear:
            decision.update(qualified=not selected.stop, review_all_ocr_fields=selected.stop)
            if selected.reason == "neither_qualified":
                decision.update(
                    selected="tesseract", stop=False, reason="neither_qualified_manual_review"
                )
        experiment.update(
            status="complete", ended_ns=time.monotonic_ns(), decision=decision, candidates=summaries
        )
        _save(output_dir / "experiment.json", experiment)
        compact = {
            "version": version,
            "host": facts,
            "decision": decision,
            "candidates": {engine: _compact(report) for engine, report in summaries.items()},
        }
        _save(output_dir / "summary.json", compact)
        print(f"OCR_FORMAL_SUMMARY_V{version}=" + json.dumps(compact), flush=True)
        for engine, report in summaries.items():
            for group in report["groups"].values():
                for document in group["documents"]:
                    print(
                        "OCR_FORMAL_DOCUMENT_V1=" + json.dumps({"engine": engine, **document}),
                        flush=True,
                    )
        return compact
    except BaseException as error:
        # A broken experiment reports every planned denominator and its actual
        # attempted slots. Unattempted requests have no invented output or time.
        for path in sorted(output_dir.glob("*.json")):
            if path.name.startswith(("text-", "hot-", "aux-")):
                partial = _json(path, maximum=67108864)
                engine = partial.get("profile", {}).get("engine")
                if engine in records:
                    seen = {(row["case_id"], row["round"]) for row in records[engine]}
                    records[engine].extend(
                        row
                        for row in partial.get("records", [])
                        if (row["case_id"], row["round"]) not in seen
                    )
        experiment.update(
            status="partial",
            ended_ns=time.monotonic_ns(),
            reason=getattr(error, "reason", "formal_benchmark_failed"),
            decision={"selected": None, "stop": True, "reason": "experiment_incomplete"},
            attempted_slots={
                engine: [{"case_id": row["case_id"], "round": row["round"]} for row in rows]
                for engine, rows in records.items()
            },
        )
        _save(output_dir / "experiment.json", experiment)
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("corpus", "assets", "output"):
        parser.add_argument("--" + name + "-dir", type=Path, required=True)
    parser.add_argument("--selection-path", type=Path, required=True)
    parser.add_argument("--clear-frozen", type=Path)
    options = parser.parse_args()
    try:
        report = run_formal(
            options.corpus_dir.resolve(),
            options.assets_dir.resolve(),
            options.output_dir.resolve(),
            options.selection_path.resolve(),
            clear_frozen=options.clear_frozen.resolve() if options.clear_frozen else None,
        )
    except Exception as error:
        print(getattr(error, "reason", "formal_benchmark_failed"), file=sys.stderr)
        return 1
    return 1 if report["decision"]["stop"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
