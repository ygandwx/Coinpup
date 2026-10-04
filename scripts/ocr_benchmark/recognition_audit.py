"""Check actual post-recognition native bytes against the same-run preflight evidence."""

import hashlib
import json
import os
import sys
from pathlib import Path

from scripts.ocr_benchmark import engine_environment as environment

PREFLIGHT_BYTES = 64 * 1024**2


class RecognitionAuditError(Exception):
    def __init__(self, reason, summary):
        self.reason, self.summary = reason, summary
        super().__init__(reason)


def _reject(value):
    raise ValueError


def _object(pairs):
    value = dict(pairs)
    if len(value) != len(pairs):
        raise ValueError
    return value


def post_recognition_audit(
    engine, preflight_report_path, observed_paths, outpath, *, observation_complete=True
):
    """Save facts before reporting failure; never infer permission from a filename."""
    report = {
        "version": 1,
        "engine": engine if engine in ("paddle", "tesseract") else None,
        "phase": "post_recognition",
        "observation_complete": observation_complete
        if type(observation_complete) is bool
        else None,
        "status": "failed",
        "reason": None,
        "native": [],
    }
    reason = None
    try:
        if (
            type(observation_complete) is not bool
            or engine not in ("paddle", "tesseract")
            or not isinstance(observed_paths, (list, tuple, set, frozenset))
        ):
            raise ValueError
        with Path(preflight_report_path).open("rb") as stream:
            data = stream.read(PREFLIGHT_BYTES + 1)
        if len(data) > PREFLIGHT_BYTES:
            raise ValueError
        report["preflight_identity"] = {
            "byte_size": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
        }
        preflight = json.loads(data, object_pairs_hook=_object, parse_constant=_reject)
        identities = [
            {"name": path.name, **environment._identity(path)} for path in environment.POLICY_PATHS
        ]
        report["native_policy_identities"] = identities
        report["provenance_identity"] = environment._identity(environment.PROVENANCE_PATH)
        if (
            type(preflight) is not dict
            or preflight.get("engine") != engine
            or preflight.get("status") != "initialized"
            or preflight.get("inference_performed") is not False
            or preflight.get("provenance_identity") != report["provenance_identity"]
            or preflight.get("native_policy_identities") != identities
        ):
            reason = "recognition_preflight_mismatch"
        else:
            policy = {"records": []}
            for path in environment.POLICY_PATHS:
                policy["records"].extend(environment._json(path)["records"])
            paths = sorted({Path(path).resolve() for path in observed_paths}, key=str)
            report["observed_paths"] = [str(path) for path in paths]
            report["native"] = environment._native_inventory(
                [Path(sys.executable), *paths], mapped_paths=paths
            )
            report["license_unresolved"] = environment._native_policy(
                report["native"], policy, environment._evidence(preflight)
            )
            if not report["native"] or any(native["missing"] for native in report["native"]):
                reason = "candidate_native_unresolved"
            elif report["license_unresolved"]:
                reason = "candidate_license_incomplete"
            else:
                report["status"] = "passed"
    except (OSError, UnicodeError, ValueError, TypeError, RecursionError):
        reason = "recognition_audit_invalid"
    except Exception:
        reason = "recognition_audit_failed"
    if observation_complete is False:
        report["status"] = "failed"
        reason = "recognition_observation_incomplete"
    report["reason"] = reason
    report["native_count"] = len(report["native"])
    summary = {key: report[key] for key in ("status", "reason", "native_count")} | {
        "report_sha256": None
    }
    try:
        target = Path(outpath)
        temporary = target.with_name(target.name + ".tmp")
        encoded = (json.dumps(report, ensure_ascii=False, indent=2) + "\n").encode("utf8")
        temporary.write_bytes(encoded)
        temporary.chmod(0o644)
        os.replace(temporary, target)
        summary["report_sha256"] = hashlib.sha256(encoded).hexdigest()
    except (OSError, ValueError, TypeError):
        summary.update(status="failed", reason="recognition_audit_output_invalid")
        raise RecognitionAuditError(summary["reason"], summary) from None
    if reason:
        raise RecognitionAuditError(reason, summary) from None
    return summary
