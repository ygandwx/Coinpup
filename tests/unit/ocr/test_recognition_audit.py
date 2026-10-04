"""Fictional native facts test identity/evidence gates without SDK imports or ldd."""

import hashlib
import json
import os
from copy import deepcopy

import pytest

from scripts.ocr_benchmark import recognition_audit as audit


@pytest.fixture
def fixture(tmp_path, monkeypatch):
    provenance = tmp_path / "provenance.json"
    provenance.write_bytes(b"fictional locked provenance")
    notice = {"name": "fixture/LICENSE", "sha256": "b" * 64}
    native = {"path": "/opt/fictional.so", "sha256": "a" * 64, "byte_size": 17, "missing": False}
    record = {
        "sha256": native["sha256"],
        "byte_size": native["byte_size"],
        "status": "allowed",
        "license_ids": ["MIT"],
        "evidence": [{"kind": "wheel", "package": "Fictional", "version": "1", **notice}],
    }
    policies = [tmp_path / "wheel.json", tmp_path / "system.json"]
    for path, rows in zip(policies, [[record], []], strict=True):
        path.write_text(json.dumps({"version": 1, "records": rows}))
    preflight = {
        "engine": "paddle",
        "status": "initialized",
        "inference_performed": False,
        "provenance_identity": audit.environment._identity(provenance),
        "native_policy_identities": [
            {"name": path.name, **audit.environment._identity(path)} for path in policies
        ],
        "installed": [{"package": "Fictional", "version": "1", "notices": [notice]}],
        "system": {
            "packages": [],
            "python_license": {"path": "/opt/python-LICENSE", "sha256": "c" * 64},
            "common_licenses": [],
        },
        "source_assets": {"files": []},
    }
    source, output = tmp_path / "preflight.json", tmp_path / "native.json"
    source.write_text(json.dumps(preflight))
    monkeypatch.setattr(audit.environment, "PROVENANCE_PATH", provenance)
    monkeypatch.setattr(audit.environment, "POLICY_PATHS", tuple(policies))
    calls = []

    def inventory(paths, *, mapped_paths):
        calls.append((paths, mapped_paths))
        return [deepcopy(native)]

    monkeypatch.setattr(audit.environment, "_native_inventory", inventory)
    return preflight, source, output, provenance, policies, native, calls


def run(fixture):
    _, source, output, _, _, _, _ = fixture
    return audit.post_recognition_audit("paddle", source, [source, str(source)], output)


def test_success_binds_current_inputs_actual_bytes_and_complete_notice_evidence(fixture):
    _, source, output, _, _, _, calls = fixture
    summary = run(fixture)
    report = json.loads(output.read_text())
    assert summary == {
        "status": "passed",
        "reason": None,
        "native_count": 1,
        "report_sha256": hashlib.sha256(output.read_bytes()).hexdigest(),
    }
    assert report["preflight_identity"] == audit.environment._identity(source)
    assert report["native"][0]["reviewed_license_policy"]["license_ids"] == ["MIT"]
    assert calls[0][1] == [source.resolve()]
    assert len(calls[0][0]) == 2
    assert "installed" not in report  # Full notices stay in the bound preflight artifact.
    if os.name != "nt":
        assert output.stat().st_mode & 0o777 == 0o644


@pytest.mark.parametrize("defect", ["engine", "status", "inference", "provenance", "policy"])
def test_stale_or_incompatible_preflight_is_rejected_before_native_work(fixture, defect):
    preflight, source, output, provenance, policies, _, calls = fixture
    if defect == "provenance":
        provenance.write_bytes(b"changed")
    elif defect == "policy":
        policies[0].write_bytes(policies[0].read_bytes() + b" ")
    else:
        preflight[
            {"engine": "engine", "status": "status", "inference": "inference_performed"}[defect]
        ] = {"engine": "tesseract", "status": "failed", "inference": True}[defect]
        source.write_text(json.dumps(preflight))
    with pytest.raises(audit.RecognitionAuditError) as caught:
        run(fixture)
    assert caught.value.reason == "recognition_preflight_mismatch"
    assert caught.value.summary["status"] == "failed" and calls == []
    assert json.loads(output.read_text())["reason"] == caught.value.reason


@pytest.mark.parametrize("defect", ["bytes", "notice", "pending", "agpl", "dynamic", "empty"])
def test_new_bytes_unknown_permission_notice_mismatch_or_missing_dynamic_never_pass(
    fixture, monkeypatch, defect
):
    preflight, source, output, _, policies, native, _ = fixture
    if defect == "notice":
        preflight["installed"][0]["notices"][0]["sha256"] = "d" * 64
    elif defect in ("pending", "agpl"):
        policy = json.loads(policies[0].read_text())
        policy["records"][0]["status" if defect == "pending" else "license_ids"] = (
            "pending" if defect == "pending" else ["MIT OR AGPL-3.0-only"]
        )
        policies[0].write_text(json.dumps(policy))
        preflight["native_policy_identities"][0] = {
            "name": policies[0].name,
            **audit.environment._identity(policies[0]),
        }
    elif defect == "bytes":
        native["sha256"] = "e" * 64
    elif defect == "dynamic":
        native["missing"] = True
    else:
        monkeypatch.setattr(audit.environment, "_native_inventory", lambda *args, **kwargs: [])
    source.write_text(json.dumps(preflight))
    with pytest.raises(audit.RecognitionAuditError) as caught:
        run(fixture)
    assert caught.value.reason == (
        "candidate_native_unresolved"
        if defect in ("dynamic", "empty")
        else "candidate_license_incomplete"
    )
    report = json.loads(output.read_text())
    assert report["status"] == "failed" and report["reason"] == caught.value.reason
    assert caught.value.summary["report_sha256"] == hashlib.sha256(output.read_bytes()).hexdigest()


@pytest.mark.parametrize(
    "payload", [b"[]", b'{"engine":"paddle","engine":"paddle"}', b'{"x":NaN}', b"\xff"]
)
def test_invalid_report_json_retains_a_stable_failure_artifact(fixture, payload):
    _, source, output, _, _, _, _ = fixture
    source.write_bytes(payload)
    with pytest.raises(audit.RecognitionAuditError) as caught:
        run(fixture)
    assert caught.value.reason in ("recognition_audit_invalid", "recognition_preflight_mismatch")
    assert json.loads(output.read_text())["status"] == "failed"


def test_preflight_read_limit_is_checked_before_parsing_or_native_work(fixture, monkeypatch):
    _, source, output, _, _, _, calls = fixture
    monkeypatch.setattr(audit, "PREFLIGHT_BYTES", 8)
    source.write_bytes(b"x" * 100)
    with pytest.raises(audit.RecognitionAuditError) as caught:
        run(fixture)
    assert caught.value.reason == "recognition_audit_invalid" and calls == []
    assert "preflight_identity" not in json.loads(output.read_text())


def test_unexpected_failure_does_not_serialize_exception_input_or_authorize(fixture, monkeypatch):
    def crash(*args, **kwargs):
        raise RuntimeError("secret/path/connection")

    monkeypatch.setattr(audit.environment, "_native_inventory", crash)
    with pytest.raises(audit.RecognitionAuditError) as caught:
        run(fixture)
    assert caught.value.reason == "recognition_audit_failed"
    assert "secret" not in fixture[2].read_text() and "secret" not in str(caught.value)


def test_unwritable_output_is_an_explicit_failure_not_a_pass(fixture):
    preflight, source, output, _, _, _, _ = fixture
    with pytest.raises(audit.RecognitionAuditError) as caught:
        audit.post_recognition_audit("paddle", source, [], output / "missing/native.json")
    assert caught.value.reason == "recognition_audit_output_invalid"
    assert (
        caught.value.summary["status"] == "failed" and caught.value.summary["report_sha256"] is None
    )


def test_incomplete_observation_preserves_available_native_facts_but_never_passes(fixture):
    _, source, output, _, _, _, calls = fixture
    with pytest.raises(audit.RecognitionAuditError) as caught:
        audit.post_recognition_audit("paddle", source, [source], output, observation_complete=False)
    report = json.loads(output.read_text())
    assert caught.value.reason == "recognition_observation_incomplete"
    assert report["observation_complete"] is False and report["status"] == "failed"
    assert len(calls) == 1 and report["native_count"] == 1
    assert report["native"][0]["reviewed_license_policy"]["status"] == "allowed"
    assert caught.value.summary["report_sha256"] == hashlib.sha256(output.read_bytes()).hexdigest()


def test_observation_complete_does_not_accept_truthy_nonboolean_values(fixture):
    _, source, output, _, _, _, calls = fixture
    with pytest.raises(audit.RecognitionAuditError) as caught:
        audit.post_recognition_audit("paddle", source, [source], output, observation_complete=1)
    assert caught.value.reason == "recognition_audit_invalid" and calls == []
    assert json.loads(output.read_text())["observation_complete"] is None
