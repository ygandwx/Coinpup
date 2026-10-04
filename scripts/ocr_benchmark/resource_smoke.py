"""Exercise actual measured DEV initialization before any formal OCR acceptance run."""

import argparse
import hashlib
import json
import os
import sys
import uuid
from pathlib import Path

from .corpus import verify_corpus
from .development_run import (
    ROOT,
    ContainerSession,
    DevelopmentError,
    _command,
    _fail,
    _save,
    development_cases,
)
from .resource_monitor import ResourceMonitorError


def run_smoke(corpus_dir, assets_dir, output_dir, selection_path):
    if sys.platform != "linux":
        _fail("candidate_platform_invalid")
    manifest = verify_corpus(corpus_dir)
    frozen = json.loads((ROOT / "tests/fixtures/ocr/corpus-frozen.json").read_text())
    if (
        hashlib.sha256((corpus_dir / "manifest.json").read_bytes()).hexdigest()
        != frozen["manifest_sha256"]
    ):
        _fail("frozen_corpus_drift")
    selection = json.loads(selection_path.read_text())
    if (
        type(selection.get("version")) is not int
        or selection["version"] != 1
        or selection.get("formal_inference_performed") is not False
        or selection.get("tesseract") != {"engine": "tesseract", "model_set": "fast", "psm": 6}
        or selection.get("paddle") != {"engine": "paddle"}
    ):
        _fail("development_selection_changed")
    case = development_cases(manifest)[0]
    output_dir.mkdir(mode=0o700, parents=True, exist_ok=False)
    staging = output_dir.with_name(output_dir.name + "-staging")
    staging.mkdir(mode=0o700)
    name = uuid.uuid4().hex + ".pdf"
    target = staging / name
    with target.open("xb") as stream:
        stream.write((corpus_dir / case["relative_path"]).read_bytes())
    target.chmod(0o600)
    if _command(["sudo", "chown", "-R", "10001:10001", str(staging)]).returncode:
        _fail("candidate_staging_invalid")
    cpus = sorted(os.sched_getaffinity(0))[:2]
    if len(cpus) != 2:
        _fail("candidate_resource_invalid")
    reports = []
    for engine in ("tesseract", "paddle"):
        audit_output = output_dir / (engine + "-native")
        audit_output.mkdir(mode=0o755)
        if _command(["sudo", "chown", "10001:10001", str(audit_output)]).returncode:
            _fail("candidate_staging_invalid")
        session = ContainerSession(
            selection[engine],
            staging.resolve(),
            assets_dir.resolve(),
            ",".join(map(str, cpus)),
            audit_output=audit_output.resolve(),
            resource_output=output_dir / (engine + "-resources.json"),
        )
        try:
            record = session.request(
                {
                    "path": "/opt/staging/" + name,
                    "sha256": case["sha256"],
                    "byte_size": case["byte_size"],
                },
                case["media_type"],
            )
            result = record["output"]
            if (
                result["status"] != "processed"
                or [page["raster_rgb_sha256"] for page in result["pages"]] != case["raster_sha256"]
            ):
                _fail("candidate_measured_recognition_failed")
            resources = session.stop_measurement()
            initialization = session.initialization_timings
            if (
                resources["status"] != "complete"
                or resources["same_window_rss_peak_bytes"] <= 0
                or not resources["samples"]
                or resources["samples"][0]["sample_end_ns"] > session.gate_sent_ns
                or type(initialization) is not dict
                or not session.gate_sent_ns
                <= initialization["start"]
                <= initialization["end"]
                <= session.ready_ns
            ):
                _fail("candidate_measurement_invalid")
            audit = session.finish()
            if (
                hashlib.sha256((audit_output / "native.json").read_bytes()).hexdigest()
                != audit["report_sha256"]
            ):
                _fail("candidate_recognition_audit_failed")
            report = {
                "version": 1,
                "scope": "development_resource_smoke_only",
                "formal_inference_performed": False,
                "profile": selection[engine],
                "metadata": session.metadata,
                "case_id": case["id"],
                "boot": session.boot,
                "started_ns": session.started_ns,
                "boot_received_ns": session.boot_received_ns,
                "gate_sent_ns": session.gate_sent_ns,
                "ready_ns": session.ready_ns,
                "initialization_timings_ns": session.initialization_timings,
                "record": record,
                "resources": resources,
                "recognition_native_audit": audit,
            }
        finally:
            session.close()
        _save(output_dir / (engine + "-smoke.json"), report)
        reports.append(report)
        print(
            "OCR_RESOURCE_SMOKE_V1="
            + json.dumps(
                {
                    "engine": engine,
                    "formal_inference_performed": False,
                    "startup_ns": report["ready_ns"] - report["started_ns"],
                    "request_ns": record["returned_ns"] - record["submitted_ns"],
                    "rss_peak_bytes": resources["same_window_rss_peak_bytes"],
                    "cgroup_memory_peak_bytes": resources["final"]["memory_peak_bytes"],
                    "cpu_usage_delta_usec": resources["cpu_usage_delta_usec"],
                    "samples": len(resources["samples"]),
                    "sample_gap_min_ns": min(resources["sample_gaps_ns"], default=None),
                    "sample_gap_max_ns": max(resources["sample_gaps_ns"], default=None),
                    "time_namespace": resources["time_namespace"],
                    "namespace_reads": resources["namespace_reads"],
                    "individual_hwm_bytes": resources["individual_hwm_bytes"],
                }
            ),
            flush=True,
        )
    return reports


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("corpus", "assets", "output"):
        parser.add_argument("--" + name + "-dir", type=Path, required=True)
    parser.add_argument("--selection-path", type=Path, required=True)
    options = parser.parse_args()
    try:
        run_smoke(
            options.corpus_dir.resolve(),
            options.assets_dir.resolve(),
            options.output_dir.resolve(),
            options.selection_path.resolve(),
        )
    except (DevelopmentError, ResourceMonitorError, OSError, ValueError) as error:
        print(getattr(error, "reason", "resource_smoke_failed"), file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
