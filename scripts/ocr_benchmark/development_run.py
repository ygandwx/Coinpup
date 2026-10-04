"""Run only independent DEV inputs in the two real, offline candidate containers."""

import argparse
import hashlib
import json
import os
import selectors
import subprocess
import sys
import time
import uuid
from fractions import Fraction
from pathlib import Path

from .corpus import verify_corpus
from .scoring import aggregate_cer, aggregate_scores, character_errors, score_fields

ROOT = Path(__file__).resolve().parents[2]
PROFILES = tuple(
    {"engine": "tesseract", "model_set": model, "psm": psm}
    for model in ("fast", "best")
    for psm in (6, 3, 11)
) + ({"engine": "paddle"},)
FRAME_BYTES = 1048576


class DevelopmentError(Exception):
    def __init__(self, reason):
        self.reason = reason
        super().__init__(reason)


def _fail(reason):
    raise DevelopmentError(reason)


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            _fail("candidate_protocol_invalid")
        result[key] = value
    return result


def _constant(value):
    _fail("candidate_protocol_invalid")


def _decode(data):
    try:
        value = json.loads(data, object_pairs_hook=_pairs, parse_constant=_constant)
    except (UnicodeError, ValueError, RecursionError):
        _fail("candidate_protocol_invalid")
    if type(value) is not dict or type(value.get("v")) is not int or value["v"] != 1:
        _fail("candidate_protocol_invalid")
    return value


def development_cases(manifest):
    cases = manifest.get("development")
    if (
        type(cases) is not list
        or len(cases) != 4
        or {case.get("template_id") for case in cases} != {"D01", "D02", "D03", "D04"}
        or any(case.get("group") != "development" for case in cases)
        or any(case.get("carrier") != "scan_pdf" for case in cases)
    ):
        _fail("development_inputs_invalid")
    return sorted(cases, key=lambda case: case["template_id"])


def _canonical(result):
    return {key: value for key, value in result.items() if key != "timings_ns"}


def summarize_profile(profile, cases, passes):
    """Score once; a second pass verifies exact outputs and never rescues the first."""
    if profile not in PROFILES or len(passes) != 2 or any(len(p) != len(cases) for p in passes):
        _fail("development_results_invalid")
    fields, characters, documents = [], [], []
    for case, first, second in zip(cases, *passes, strict=True):
        if _canonical(first) != _canonical(second):
            _fail("candidate_output_unstable")
        if first.get("status") not in ("processed", "manual", "failed"):
            _fail("development_results_invalid")
        processed = first["status"] == "processed"
        predicted = (
            [
                {key: field[key] for key in ("path", "value", "status")}
                for field in first["parsed"]["fields"]
            ]
            if processed
            else []
        )
        text = first["raw_text"] if processed else ""
        field_score = score_fields(case["expected_fields"], predicted)
        character_score = character_errors(case["reference_text"], text)
        fields.append(field_score)
        characters.append(character_score)
        documents.append(
            {
                "case_id": case["id"],
                "status": first["status"],
                "C": field_score.correct,
                "T": field_score.expected,
                "E": field_score.extra,
                "edits": character_score.edits,
                "reference_chars": character_score.reference_chars,
            }
        )
    score, cer = aggregate_scores(fields), aggregate_cer(characters)
    return {
        "profile": dict(profile),
        "C": score.correct,
        "T": score.expected,
        "E": score.extra,
        "edits": cer["edits"],
        "reference_chars": cer["reference_chars"],
        "stable_passes": 2,
        "documents": documents,
    }


def select_tesseract(reports):
    """DEV only: accuracy, CER, fast, then the fixed PSM order 6 / 3 / 11."""
    reports = [report for report in reports if report["profile"]["engine"] == "tesseract"]
    if len(reports) != 6 or {json.dumps(r["profile"], sort_keys=True) for r in reports} != {
        json.dumps(profile, sort_keys=True) for profile in PROFILES[:-1]
    }:
        _fail("development_results_invalid")

    def key(report):
        denominator = report["T"] + report["E"]
        if denominator <= 0 or report["reference_chars"] <= 0:
            _fail("development_results_invalid")
        return (
            Fraction(report["C"], denominator),
            -Fraction(report["edits"], report["reference_chars"]),
            report["profile"]["model_set"] == "fast",
            -(6, 3, 11).index(report["profile"]["psm"]),
        )

    return dict(max(reports, key=key)["profile"])


def _command(arguments, *, timeout=30):
    try:
        return subprocess.run(arguments, capture_output=True, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired):
        _fail("candidate_container_control_failed")


class ContainerSession:
    """Bounded serial NDJSON; sources are private random names, never template identities."""

    def __init__(self, profile, staging, assets, cpus, *, audit_output=None):
        if sys.platform != "linux" or profile not in PROFILES:
            _fail("candidate_platform_invalid")
        self.name = "coinpup-dev-" + uuid.uuid4().hex
        self.profile, self.buffer, self.stderr = profile, bytearray(), bytearray()
        self.stderr_bytes, self.request_id, self.audit_finished = 0, 0, False
        self.selector = selectors.DefaultSelector()
        self.process = None
        self.started_ns = time.monotonic_ns()
        preflight = assets.parent / "candidate-reports" / profile["engine"] / "report.json"
        audit_output = audit_output or staging.parent / "native-audit"
        arguments = [
            "docker",
            "run",
            "--rm",
            "-i",
            "--name",
            self.name,
            "--init",
            "--network",
            "none",
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--cpuset-cpus",
            cpus,
            "--cpus",
            "2",
            "--memory",
            "4g",
            "--memory-swap",
            "4g",
            "--pids-limit",
            "128",
            "--tmpfs",
            "/tmp:rw,noexec,nosuid,nodev,size=512m,mode=1777",
            "-e",
            "HOME=/tmp/ocr-home",
            "--mount",
            f"type=bind,src={assets},dst=/opt/assets,readonly",
            "--mount",
            f"type=bind,src={staging},dst=/opt/staging,readonly",
            "--mount",
            f"type=bind,src={preflight},dst=/opt/candidate-audit/report.json,readonly",
            "--mount",
            f"type=bind,src={audit_output},dst=/audit",
            f"coinpup-ocr-{profile['engine']}:ci",
            "python",
            "-m",
            "scripts.ocr_benchmark.candidate_session",
            "--assets-dir",
            "/opt/assets",
            "--profile",
            json.dumps(profile, separators=(",", ":")),
            "--preflight-report",
            "/opt/candidate-audit/report.json",
            "--audit-output",
            "/audit/native.json",
        ]
        try:
            self.process = subprocess.Popen(
                arguments,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=0,
                start_new_session=True,
            )
            for stream in (self.process.stdout, self.process.stderr):
                self.selector.register(stream, selectors.EVENT_READ)
            frame = self._frame(time.monotonic() + 120)
            if frame.get("event") != "ready" or frame.get("metadata", {}).get("profile") != profile:
                _fail("candidate_startup_failed")
            self.metadata, self.ready_ns = frame["metadata"], time.monotonic_ns()
        except BaseException:
            self.close()
            raise

    def _frame(self, deadline):
        while True:
            newline = self.buffer.find(b"\n")
            if newline >= 0:
                if newline > FRAME_BYTES:
                    _fail("candidate_output_limit")
                line = bytes(self.buffer[:newline])
                del self.buffer[: newline + 1]
                return _decode(line)
            if len(self.buffer) > FRAME_BYTES:
                _fail("candidate_output_limit")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                _fail("candidate_timeout")
            for key, _ in self.selector.select(min(remaining, 0.05)):
                chunk = os.read(key.fileobj.fileno(), 65536)
                if not chunk:
                    self.selector.unregister(key.fileobj)
                    if key.fileobj is self.process.stdout:
                        _fail("candidate_process_failed")
                    continue
                if key.fileobj is self.process.stdout:
                    self.buffer.extend(chunk)
                else:
                    self.stderr_bytes += len(chunk)
                    if self.stderr_bytes > FRAME_BYTES:
                        _fail("candidate_diagnostic_limit")
                    self.stderr.extend(chunk)
                    del self.stderr[:-8192]

    def request(self, source, media_type):
        self.request_id += 1
        request = {"v": 1, "id": self.request_id, "source": source, "media_type": media_type}
        encoded = json.dumps(request, allow_nan=False).encode("utf8") + b"\n"
        if len(encoded) > FRAME_BYTES:
            _fail("candidate_protocol_invalid")
        submitted_ns = time.monotonic_ns()
        self.process.stdin.write(encoded)
        self.process.stdin.flush()
        deadline, pages = time.monotonic() + 60, set()
        while True:
            frame = self._frame(deadline)
            if type(frame.get("id")) is not int or frame["id"] != self.request_id:
                _fail("candidate_protocol_invalid")

            if frame.get("event") == "page_done":
                index = frame.get("page_index")
                if type(index) is not int or not 0 <= index < 50 or index in pages:
                    _fail("candidate_protocol_invalid")
                pages.add(index)
                deadline = time.monotonic() + 60
            elif frame.get("event") == "result":
                result = frame.get("result")
                items = result.get("pages") if type(result) is dict else None
                if type(items) is not list or any(type(p) is not dict for p in items):
                    _fail("candidate_protocol_invalid")
                indices = [p.get("page_index") for p in items]
                if (
                    any(type(index) is not int or not 0 <= index < 50 for index in indices)
                    or len(set(indices)) != len(indices)
                    or set(indices) != pages
                ):
                    _fail("candidate_protocol_invalid")
                return {
                    "output": result,
                    "submitted_ns": submitted_ns,
                    "returned_ns": time.monotonic_ns(),
                }
            else:
                _fail("candidate_protocol_invalid")

    def finish(self):
        """Explicitly await actual loaded-native audit before permitting DEV selection."""
        self.request_id += 1
        request = {"v": 1, "id": self.request_id, "action": "finish"}
        self.process.stdin.write(json.dumps(request).encode("utf8") + b"\n")
        self.process.stdin.flush()
        frame = self._frame(time.monotonic() + 120)
        summary = frame.get("summary")
        if (
            type(frame.get("id")) is not int
            or frame["id"] != self.request_id
            or frame.get("event") != "audit_done"
            or type(summary) is not dict
            or summary.get("status") != "passed"
            or type(summary.get("native_count")) is not int
            or summary["native_count"] <= 0
        ):
            _fail("candidate_recognition_audit_failed")
        self.audit_finished = True
        return summary

    def close(self):
        if self.process is None:
            self.selector.close()
            return
        if self.process.stdin and not self.process.stdin.closed:
            self.process.stdin.close()
        try:
            self.process.wait(timeout=20)
        except subprocess.TimeoutExpired:
            pass
        try:
            removed = _command(["docker", "rm", "--force", self.name], timeout=20)
            inspected = _command(["docker", "inspect", self.name], timeout=20)
            if inspected.returncode != 1 or inspected.stdout.strip() != b"[]":
                _fail("candidate_cleanup_unconfirmed")
            if removed.returncode not in (0, 1):
                _fail("candidate_cleanup_unconfirmed")
        finally:
            try:
                if self.process.poll() is None:
                    self.process.kill()
                exit_code = self.process.wait(timeout=20)
            finally:
                for stream in (self.process.stdout, self.process.stderr):
                    stream.close()
                self.selector.close()
                self.process = None
        if self.audit_finished and exit_code != 0:
            _fail("candidate_process_failed")


def _save(path, value):
    path.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf8"
    )


def run_development(corpus_dir, assets_dir, output_dir):
    if sys.platform != "linux":
        _fail("candidate_platform_invalid")
    manifest = verify_corpus(corpus_dir)
    frozen = json.loads((ROOT / "tests/fixtures/ocr/corpus-frozen.json").read_text())
    if (
        hashlib.sha256((corpus_dir / "manifest.json").read_bytes()).hexdigest()
        != frozen["manifest_sha256"]
    ):
        _fail("frozen_corpus_drift")
    cases = development_cases(manifest)
    output_dir.mkdir(mode=0o700, parents=True, exist_ok=False)
    # Private source permissions must not obstruct traversal of public report artifacts.
    staging = output_dir.with_name(output_dir.name + "-staging")
    staging.mkdir(mode=0o700)
    sources = []
    for case in cases:
        name = uuid.uuid4().hex + ".pdf"
        target = staging / name
        with target.open("xb") as stream:
            stream.write((corpus_dir / case["relative_path"]).read_bytes())
        target.chmod(0o600)
        sources.append(
            {
                "path": "/opt/staging/" + name,
                "sha256": case["sha256"],
                "byte_size": case["byte_size"],
            }
        )
    # Only this fresh, explicitly fictional staging subtree changes owner.
    owned = _command(["sudo", "chown", "-R", "10001:10001", str(staging)])
    if owned.returncode:
        _fail("candidate_staging_invalid")
    cpus = sorted(os.sched_getaffinity(0))[:2]
    if len(cpus) != 2:
        _fail("candidate_resource_invalid")
    reports = []
    for profile in PROFILES:
        identity = (
            "paddle"
            if profile["engine"] == "paddle"
            else f"tesseract-{profile['model_set']}-psm{profile['psm']}"
        )
        native_output = output_dir / ("native-" + identity)
        native_output.mkdir(mode=0o755)
        owned = _command(["sudo", "chown", "10001:10001", str(native_output)])
        if owned.returncode:
            _fail("candidate_staging_invalid")
        session = ContainerSession(
            profile,
            staging.resolve(),
            assets_dir.resolve(),
            ",".join(map(str, cpus)),
            audit_output=native_output.resolve(),
        )
        passes, records = [], []
        try:
            for repeat in range(2):
                outputs = []
                for case, source in zip(cases, sources, strict=True):
                    record = session.request(source, case["media_type"])
                    output = record["output"]
                    if (
                        output["status"] == "processed"
                        and [p["raster_rgb_sha256"] for p in output["pages"]]
                        != case["raster_sha256"]
                    ):
                        _fail("candidate_raster_drift")
                    outputs.append(output)
                    records.append({"case_id": case["id"], "pass": repeat, **record})
                passes.append(outputs)
            audit = session.finish()
            if (
                hashlib.sha256((native_output / "native.json").read_bytes()).hexdigest()
                != audit["report_sha256"]
            ):
                _fail("candidate_recognition_audit_failed")
            report = summarize_profile(profile, cases, passes)
            report.update(
                metadata=session.metadata,
                startup_ns=session.ready_ns - session.started_ns,
                records=records,
                recognition_native_audit=audit,
                diagnostics={
                    "stderr_bytes": session.stderr_bytes,
                    "tail": bytes(session.stderr).decode("utf8", errors="replace"),
                },
            )
        finally:
            session.close()
        _save(output_dir / (identity + ".json"), report)
        reports.append(report)
        print(
            "OCR_DEV_RESULT_V1="
            + json.dumps(
                {
                    key: report[key]
                    for key in (
                        "profile",
                        "C",
                        "T",
                        "E",
                        "edits",
                        "reference_chars",
                        "stable_passes",
                    )
                }
            ),
            flush=True,
        )
    selection = {
        "version": 1,
        "tesseract": select_tesseract(reports),
        "paddle": {"engine": "paddle"},
        "selection_rule": "accuracy_then_CER_then_fast_then_PSM_6_3_11",
        "formal_inference_performed": False,
    }
    _save(output_dir / "selection.json", selection)
    print("OCR_DEV_SELECTION_V1=" + json.dumps(selection), flush=True)
    return selection


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus-dir", type=Path, required=True)
    parser.add_argument("--assets-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    options = parser.parse_args()
    try:
        run_development(
            options.corpus_dir.resolve(), options.assets_dir.resolve(), options.output_dir.resolve()
        )
    except (DevelopmentError, OSError, ValueError) as error:
        print(
            error.reason if isinstance(error, DevelopmentError) else "development_run_failed",
            file=sys.stderr,
        )
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
