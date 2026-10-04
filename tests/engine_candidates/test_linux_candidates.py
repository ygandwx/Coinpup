"""Verify evidence produced by the real, network-disabled Linux candidate containers."""

import hashlib
import json
import os
import sys
from pathlib import Path

import pytest

from scripts.ocr_benchmark import engine_environment as environment

pytestmark = [pytest.mark.ocr, pytest.mark.engine_candidates]
ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module", params=["paddle", "tesseract"])
def report(request):
    assert sys.platform == "linux", "The real candidate profile requires Linux."
    configured = os.environ.get("COINPUP_CANDIDATE_REPORTS")
    assert configured, "COINPUP_CANDIDATE_REPORTS must identify the real container audit outputs."
    path = Path(configured) / request.param / "report.json"
    assert path.is_file(), "The candidate container must produce its real audit report."
    value = json.loads(path.read_text(encoding="utf8"))
    assert value["engine"] == request.param and value["version"] == 1
    return value


def test_actual_initialization_and_offline_finite_resources(report, capsys):
    assert report["status"] == "initialized" and report["reason"] is None
    assert report["initialization_attempted"] is True and report["inference_performed"] is False
    runtime = report["runtime"]
    assert runtime["machine"] == "x86_64" and runtime["python"] == "3.12.14"
    assert runtime["interfaces"] == ["lo"] and len(set(runtime["affinity"])) == 2
    assert runtime["cgroup"] == {
        "memory.max": "4294967296",
        "memory.swap.max": "0",
        "pids.max": "128",
    }
    assert all(value == "1" for value in report["thread_environment"].values())
    assert "No broken requirements found" in report["pip_check"]
    initialized = report["initialization"]
    assert initialized["loaded_native"]
    if report["engine"] == "paddle":
        assert initialized["versions"] == {
            "paddle": "3.4.0",
            "paddleocr": "3.7.0",
            "paddlex": "3.7.0",
            "cv2": "4.10.0",
        }
        assert initialized["arguments"]["cpu_threads"] == 1
        assert initialized["arguments"]["device"] == "cpu"
        assert all(
            initialized["arguments"][name] is False
            for name in (
                "enable_hpi",
                "use_doc_orientation_classify",
                "use_doc_unwarping",
                "use_textline_orientation",
            )
        )
    else:
        assert initialized["versions"] == {"tesseract": "5.5.3", "leptonica": "1.86.0"}
        assert initialized["languages"] == ["eng", "chi_sim", "chi_tra"]
        assert initialized["model_sets"] == ["fast", "best"]
    with capsys.disabled():
        print(
            "CANDIDATE_INITIALIZED "
            + json.dumps(
                {
                    "engine": report["engine"],
                    "versions": initialized["versions"],
                    "native": len(report["native"]),
                    "inference": False,
                },
                sort_keys=True,
            )
        )


def test_actual_models_and_installed_notices_match_frozen_sources(report):
    assets_dir = os.environ.get("COINPUP_ENGINE_ASSETS")
    assert assets_dir, (
        "The original pinned model inputs must remain available for independent checks."
    )
    manifest = json.loads(
        (ROOT / "tests/fixtures/ocr/engine-assets.json").read_text(encoding="utf8")
    )
    selected = {
        item["name"]: item
        for item in manifest["files"]
        if item["role"] == ("model" if report["engine"] == "paddle" else "traineddata")
    }
    assert {item["name"] for item in report["models"]} == set(selected)
    for item in report["models"]:
        assert item == {key: selected[item["name"]][key] for key in ("name", "sha256", "byte_size")}
        path = Path(assets_dir) / item["name"]
        assert environment._identity(path) == {key: item[key] for key in ("sha256", "byte_size")}
    provenance = ROOT / "ops/ocr-benchmark/paddle-provenance.json"
    assert report["provenance_identity"] == environment._identity(provenance)
    locked = {
        item["package"].lower(): item
        for item in json.loads(provenance.read_text(encoding="utf8"))["records"]
    }
    for distribution in report["installed"]:
        source = locked[distribution["package"].lower()]
        assert distribution["version"] == source["version"]
        assert distribution["wheel_sha256"] == source["sha256"]
        for item in [distribution["metadata"], distribution["wheel"], *distribution["notices"]]:
            data = item["text"].encode(item["encoding"])
            assert (
                len(data) == item["byte_size"]
                and hashlib.sha256(data).hexdigest() == item["sha256"]
            )
        assert distribution["reviewed_license_policy"]["status"] == "allowed"


def test_actual_loaded_and_ldd_closure_has_no_missing_or_unreviewed_native(report):
    assert not report["license_unresolved"] and not report["distribution_unresolved"]
    paths = {item["path"] for item in report["native"]}
    assert len(paths) == len(report["native"]) > 5
    assert set(report["initialization"]["loaded_native"]).issubset(paths)
    records = []
    for identity in report["native_policy_identities"]:
        path = ROOT / "ops/ocr-benchmark" / identity["name"]
        assert {key: identity[key] for key in ("sha256", "byte_size")} == environment._identity(
            path
        )
        records.extend(json.loads(path.read_text(encoding="utf8"))["records"])
    assert (
        environment._native_policy(
            report["native"], {"records": records}, environment._evidence(report)
        )
        == []
    )
    for item in report["native"]:
        assert item["missing"] is False and "not found" not in item["ldd"]
        assert set(item["dependencies"]).issubset(paths)
        assert item["reviewed_license_policy"]["status"] == "allowed"
