"""Fictional audit facts; only the separate container job runs real candidates."""

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts.ocr_benchmark import engine_environment as environment


def rejected(code):
    return pytest.raises(environment.CandidateEnvironmentError, match="^" + code + "$")


def test_missing_models_rejected_before_import_or_initializer(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(environment, "_paddle", lambda *args: calls.append("initialized"))
    monkeypatch.setattr(
        environment.importlib, "import_module", lambda *args: calls.append("imported")
    )
    with rejected("candidate_models_invalid"):
        environment.audit_environment("paddle", tmp_path / "missing", tmp_path / "audit")
    report = json.loads((tmp_path / "audit/report.json").read_text())
    assert calls == [] and report["initialization_attempted"] is False
    assert report["inference_performed"] is False and report["status"] == "failed"


@pytest.mark.parametrize(
    "defect",
    [
        None,
        "metadata",
        "notice",
        "version",
        "source",
        "license",
        "agpl",
        "agpl_or",
        "agpl_and",
        "latin1",
    ],
)
def test_installed_full_notices_and_metadata_match_actual_bytes(tmp_path, monkeypatch, defect):
    data = {
        "fixture.dist-info/METADATA": b"Name: Fictional\nVersion: 1\n",
        "fixture.dist-info/WHEEL": b"Tag: py3-none-any\n",
        "fixture.dist-info/LICENSE": b"MPL defines an AGPL Secondary License.\n",
    }
    members = []
    for name, value in data.items():
        path = tmp_path / name
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(value)
        members.append(
            {"name": name, "sha256": hashlib.sha256(value).hexdigest(), "byte_size": len(value)}
        )
    source_notice = {
        "name": "official_notice",
        "encoding": "utf-8",
        "text": "Original",
        "byte_size": 8,
        "sha256": hashlib.sha256(b"Original").hexdigest(),
    }
    record = {
        "package": "Fictional",
        "version": "1",
        "sha256": "a" * 64,
        "metadata": members[0],
        "wheel": members[1],
        "notices": members[2:],
        "source_notices": [source_notice],
        "reviewed_license_policy": {
            "status": "allowed",
            "license_ids": ["MPL-2.0"],
            "evidence": ["fixture.dist-info/LICENSE"],
        },
    }
    distribution = SimpleNamespace(
        metadata={"Name": "Fictional"},
        version="1",
        files=[],
        locate_file=lambda name: tmp_path / name,
    )
    if defect in ("metadata", "notice"):
        name = "METADATA" if defect == "metadata" else "LICENSE"
        (tmp_path / "fixture.dist-info" / name).write_bytes(b"Tampered")
    elif defect == "version":
        distribution.version = "2"
    elif defect == "source":
        source_notice["text"] = "Changed!"
    elif defect == "license":
        record["reviewed_license_policy"]["status"] = "unreviewed"
    elif defect in ("agpl", "agpl_or", "agpl_and"):
        record["reviewed_license_policy"]["license_ids"] = [
            {
                "agpl": "AGPL-3.0-only",
                "agpl_or": "(AGPL-3.0-only OR MIT)",
                "agpl_and": "Apache-2.0 AND AGPL-3.0-only",
            }[defect]
        ]
    elif defect == "latin1":
        value = b"Original notice with \xe9 preserved.\n"
        (tmp_path / "fixture.dist-info/LICENSE").write_bytes(value)
        record["notices"][0].update(sha256=hashlib.sha256(value).hexdigest(), byte_size=len(value))
    monkeypatch.setattr(environment.importlib.metadata, "distributions", lambda: [distribution])
    installed, _, problems = environment._installed({"records": [record]})
    assert len(installed) == 1
    if defect in (None, "latin1"):
        assert problems == {"integrity": [], "license": [], "forbidden": []}
        notice = installed[0]["notices"][0]
        actual = (tmp_path / "fixture.dist-info/LICENSE").read_bytes()
        assert notice["text"].encode(notice["encoding"]) == actual
    elif defect in ("metadata", "notice", "version", "source"):
        assert problems["integrity"] == ["Fictional"]
    else:
        assert problems["license"] == ["Fictional"]
        assert problems["forbidden"] == (["Fictional"] if defect.startswith("agpl") else [])


@pytest.mark.parametrize(
    "defect", [None, "pending", "agpl", "agpl_or", "agpl_and", "hash", "notice", "package"]
)
def test_native_policy_binds_native_bytes_and_full_source_notice(defect):
    record = {"path": "/fictional/library.so", "sha256": "a" * 64, "byte_size": 123}
    reference = {
        "kind": "system",
        "name": "/copyright",
        "sha256": "b" * 64,
        "package": "fictional",
        "version": "1",
    }
    policy = {
        "sha256": "a" * 64,
        "byte_size": 123,
        "status": "allowed",
        "license_ids": ["GPL-3.0-with-GCC-exception"],
        "evidence": [reference],
    }
    observed = {("system", "fictional", "1", "/copyright", "b" * 64)}
    if defect == "pending":
        policy["status"] = "pending"
    elif defect in ("agpl", "agpl_or", "agpl_and"):
        policy["license_ids"] = [
            {
                "agpl": "AGPL-3.0-or-later",
                "agpl_or": "(AGPL-3.0-only OR MIT)",
                "agpl_and": "Apache-2.0 AND AGPL-3.0-only",
            }[defect]
        ]
    elif defect == "hash":
        policy["sha256"] = "c" * 64
    elif defect == "notice":
        reference["sha256"] = "c" * 64
    elif defect == "package":
        reference["package"] = "another"
    missing = environment._native_policy([record], {"records": [policy]}, observed)
    assert missing == ([] if defect is None else ["/fictional/library.so"])


@pytest.mark.parametrize("defect", [None, "network", "memory", "swap", "pids", "affinity"])
def test_runtime_requires_offline_two_cpu_and_finite_caps(monkeypatch, defect):
    content = {
        "/proc/net/dev": "lo: 0\n",
        "/sys/fs/cgroup/memory.max": "4294967296",
        "/sys/fs/cgroup/memory.swap.max": "0",
        "/sys/fs/cgroup/pids.max": "128",
    }
    affinity = {2, 3}
    if defect == "network":
        content["/proc/net/dev"] += "eth0: 0\n"
    elif defect in ("memory", "swap", "pids"):
        key = {"memory": "memory.max", "swap": "memory.swap.max", "pids": "pids.max"}[defect]
        content["/sys/fs/cgroup/" + key] = "max"
    elif defect == "affinity":
        affinity = {2, 3, 4}
    monkeypatch.setattr(environment.sys, "platform", "linux")
    monkeypatch.setattr(environment.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(Path, "read_text", lambda self, **kwargs: content[self.as_posix()])
    monkeypatch.setattr(environment.os, "sched_getaffinity", lambda pid: affinity, raising=False)
    if defect:
        with rejected(
            "candidate_network_enabled" if defect == "network" else "candidate_resource_invalid"
        ):
            environment._runtime()
    else:
        assert environment._runtime()["affinity"] == [2, 3]


def test_paddle_uses_local_small_models_and_closes_without_inference(tmp_path, monkeypatch):
    calls = []

    def construct(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(
            close=lambda: calls.append("closed"),
            predict=lambda *args: pytest.fail("No inference in this package"),
        )

    versions = {"paddle": "3.4.0", "paddlex": "3.7.0", "cv2": "4.10.0", "paddleocr": "3.7.0"}
    modules = {name: SimpleNamespace(__version__=version) for name, version in versions.items()}
    modules["paddleocr"].PaddleOCR = construct
    monkeypatch.setattr(environment.importlib, "import_module", lambda name: modules[name])
    monkeypatch.setattr(environment, "_loaded_native", lambda: [])
    assert environment._paddle(tmp_path)["model_sets"] == ["small_det", "small_rec"]
    assert calls[-1] == "closed"
    assert calls[0]["device"] == "cpu" and calls[0]["cpu_threads"] == 1
    assert calls[0]["text_detection_model_dir"] == str(tmp_path / "models/det")
    assert calls[0]["text_recognition_model_dir"] == str(tmp_path / "models/rec")
    assert all(
        calls[0][name] is False
        for name in (
            "enable_hpi",
            "use_doc_orientation_classify",
            "use_doc_unwarping",
            "use_textline_orientation",
        )
    )


@pytest.mark.parametrize("fail", [False, True])
def test_tesseract_capi_contract_initializes_both_sets_and_always_disposes(
    tmp_path, monkeypatch, fail
):
    calls = []

    def initialize(handle, path, languages):
        calls.append((path, languages))
        return int(fail)

    library = SimpleNamespace(
        TessBaseAPICreate=lambda: 123,
        TessBaseAPIInit3=initialize,
        TessBaseAPIEnd=lambda handle: calls.append("end"),
        TessBaseAPIDelete=lambda handle: calls.append("delete"),
    )
    monkeypatch.setattr(environment.ctypes, "CDLL", lambda path: library)
    monkeypatch.setattr(environment, "_command", lambda args: "tesseract 5.5.3\nleptonica-1.86.0\n")
    monkeypatch.setattr(environment, "_loaded_native", lambda: [])
    if fail:
        with rejected("candidate_initialization_failed"):
            environment._tesseract(tmp_path)
        assert calls[-2:] == ["end", "delete"]
    else:
        result = environment._tesseract(tmp_path)
        assert result["languages"] == ["eng", "chi_sim", "chi_tra"]
        expected = []
        for kind in ("fast", "best"):
            expected.extend(
                [
                    (
                        environment.os.fsencode(tmp_path / "tesseract" / kind),
                        b"eng+chi_sim+chi_tra",
                    ),
                    "end",
                    "delete",
                ]
            )
        assert calls == expected


def test_cli_missing_models_is_stable_without_private_inputs(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(
        environment.sys,
        "argv",
        [
            "environment",
            "audit",
            "--engine",
            "paddle",
            "--assets-dir",
            str(tmp_path / "fictional-private-models"),
            "--output-dir",
            str(tmp_path / "audit"),
        ],
    )
    with pytest.raises(SystemExit) as caught:
        environment.main()
    assert caught.value.code == 1
    captured = capsys.readouterr()
    assert captured.out == "" and captured.err == "candidate_models_invalid\n"


@pytest.mark.parametrize("active_pending", [False, True])
def test_audit_preserves_initialization_facts_and_gates_only_active_native_closure(
    tmp_path, monkeypatch, active_pending
):
    for name in (*environment.THREADS, "PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK"):
        monkeypatch.setenv(name, "fictional_previous_value")
    active, inactive = tmp_path / "active.so", tmp_path / "unused.so"
    active.write_bytes(b"fictional active native")
    inactive.write_bytes(b"fictional unused native")
    manifest = {"files": [{"name": "LICENSE", "sha256": "a" * 64}]}
    monkeypatch.setattr(environment, "_models", lambda *args: [])
    monkeypatch.setattr(environment.engine_assets, "_manifest", lambda: manifest)
    monkeypatch.setattr(environment, "_runtime", lambda: {})
    monkeypatch.setattr(environment, "_command", lambda args: "No broken requirements found.\n")
    installed = [
        {"package": name, "version": version, "notices": []}
        for name, version in environment.VERSIONS.items()
    ]
    monkeypatch.setattr(
        environment,
        "_installed",
        lambda value: (installed, [inactive], {"integrity": [], "license": [], "forbidden": []}),
    )
    monkeypatch.setattr(
        environment,
        "_system_inventory",
        lambda: {
            "packages": [],
            "python_license": {"path": "LICENSE.txt", "sha256": "b" * 64},
            "common_licenses": [],
        },
    )
    monkeypatch.setattr(environment, "_loaded_native", lambda: [active])
    observed_roots, calls = [], []

    def native(paths):
        observed_roots.extend(paths)
        return [{"path": str(active), **environment._identity(active), "missing": False}]

    def initialize(path):
        calls.append("initialized")
        return {"loaded_native": [str(active)]}

    monkeypatch.setattr(environment, "_native_inventory", native)
    monkeypatch.setattr(environment, "_paddle", initialize)
    provenance, policy = tmp_path / "provenance.json", tmp_path / "policy.json"
    provenance.write_text('{"version":1,"records":[]}', encoding="utf8")
    evidence = [{"kind": "asset", "name": "LICENSE", "sha256": "a" * 64}]
    policy.write_text(
        json.dumps(
            {
                "version": 1,
                "records": [
                    {
                        **environment._identity(active),
                        "status": "pending" if active_pending else "allowed",
                        "license_ids": ["MIT"],
                        "evidence": evidence,
                    },
                    {
                        **environment._identity(inactive),
                        "status": "pending",
                        "license_ids": [],
                        "evidence": [],
                    },
                ],
            }
        ),
        encoding="utf8",
    )
    monkeypatch.setattr(environment, "PROVENANCE_PATH", provenance)
    monkeypatch.setattr(environment, "POLICY_PATHS", (policy,))
    if active_pending:
        with rejected("candidate_license_incomplete"):
            environment.audit_environment("paddle", tmp_path, tmp_path / "audit")
    else:
        assert (
            environment.audit_environment("paddle", tmp_path, tmp_path / "audit")["status"]
            == "initialized"
        )
    report = json.loads((tmp_path / "audit/report.json").read_text())
    assert calls == ["initialized"] and report["initialization_attempted"] is True
    assert report["initialization"] and report["inference_performed"] is False
    assert inactive not in observed_roots
    assert report["status"] == ("failed" if active_pending else "initialized")
