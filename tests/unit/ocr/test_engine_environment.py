"""Fictional audit facts; only the separate container job runs real candidates."""

import hashlib
import json
from pathlib import Path, PurePosixPath
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


@pytest.mark.parametrize("threads", [(1, 1), (4,), (1, 4)])
def test_paddle_uses_local_small_models_and_closes_without_inference(
    tmp_path, monkeypatch, threads
):
    calls = []
    thread_calls, observed_threads = [], iter(threads)

    def construct(**kwargs):
        assert thread_calls == [1]
        calls.append(kwargs)
        return SimpleNamespace(
            close=lambda: calls.append("closed"),
            predict=lambda *args: pytest.fail("No inference in this package"),
        )

    versions = {"paddle": "3.4.0", "paddlex": "3.7.0", "cv2": "4.10.0", "paddleocr": "3.7.0"}
    modules = {name: SimpleNamespace(__version__=version) for name, version in versions.items()}
    modules["cv2"].setNumThreads = lambda count: thread_calls.append(count)
    modules["cv2"].getNumThreads = lambda: next(observed_threads)
    modules["paddleocr"].PaddleOCR = construct
    monkeypatch.setattr(environment.importlib, "import_module", lambda name: modules[name])
    monkeypatch.setattr(environment, "_loaded_native", lambda: [])
    if threads != (1, 1):
        with rejected("candidate_resource_invalid"):
            environment._paddle(tmp_path)
        assert thread_calls == [1]
        assert calls == [] if threads == (4,) else calls[-1] == "closed"
        return
    result = environment._paddle(tmp_path)
    assert result["model_sets"] == ["small_det", "small_rec"]
    assert result["opencv_threads"] == 1 and thread_calls == [1]
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


def _language_array(names, *, terminated=True):
    buffers = [environment.ctypes.create_string_buffer(name) for name in names]
    addresses = [environment.ctypes.addressof(buffer) for buffer in buffers]
    if terminated:
        addresses.append(None)
    return buffers, (environment.ctypes.c_void_p * len(addresses))(*addresses)


@pytest.mark.parametrize("fail", [False, True])
def test_tesseract_capi_contract_initializes_both_sets_and_always_disposes(
    tmp_path, monkeypatch, fail
):
    calls = []

    def initialize(handle, path, languages):
        calls.append((path, languages))
        return int(fail)

    buffers, languages = _language_array([b"eng", b"chi_sim", b"chi_tra"])
    library = SimpleNamespace(
        TessBaseAPICreate=lambda: 123,
        TessBaseAPIInit3=initialize,
        TessBaseAPIGetLoadedLanguagesAsVector=lambda handle: languages,
        TessDeleteTextArray=lambda values: calls.append("free_languages"),
        TessBaseAPIEnd=lambda handle: calls.append("end"),
        TessBaseAPIDelete=lambda handle: calls.append("delete"),
    )

    def load_library(path):
        assert path == "/opt/tesseract/lib/libtesseract.so.5.5.3"
        return library

    monkeypatch.setattr(environment.ctypes, "CDLL", load_library)
    monkeypatch.setattr(environment, "_command", lambda args: "tesseract 5.5.3\nleptonica-1.86.0\n")
    monkeypatch.setattr(environment, "_loaded_native", lambda: [])
    if fail:
        with rejected("candidate_initialization_failed"):
            environment._tesseract(tmp_path)
        assert calls[-2:] == ["end", "delete"]
    else:
        result = environment._tesseract(tmp_path)
        assert result["languages"] == ["eng", "chi_sim", "chi_tra"]
        assert result["loaded_languages"] == {
            "fast": ["eng", "chi_sim", "chi_tra"],
            "best": ["eng", "chi_sim", "chi_tra"],
        }
        assert buffers and library.TessBaseAPIGetLoadedLanguagesAsVector.argtypes == [
            environment.ctypes.c_void_p
        ]
        assert library.TessDeleteTextArray.argtypes == [
            environment.ctypes.POINTER(environment.ctypes.c_void_p)
        ]
        expected = []
        for kind in ("fast", "best"):
            expected.extend(
                [
                    (
                        environment.os.fsencode(tmp_path / "tesseract" / kind),
                        b"eng+chi_sim+chi_tra",
                    ),
                    "free_languages",
                    "end",
                    "delete",
                ]
            )
        assert calls == expected


@pytest.mark.parametrize("kind", ["fast", "best"])
@pytest.mark.parametrize("missing", [b"eng", b"chi_sim", b"chi_tra"])
def test_tesseract_success_code_does_not_hide_missing_explicit_language(
    tmp_path, monkeypatch, kind, missing
):
    required = [b"eng", b"chi_sim", b"chi_tra"]
    _check_tesseract_language_failure(
        tmp_path, monkeypatch, [name for name in required if name != missing], kind=kind
    )


@pytest.mark.parametrize(
    "names,terminated",
    [
        (None, True),
        ([], True),
        ([b"eng", b"chi_sim", b"chi_tra", b"\xff"], True),
        ([b"eng", b"chi_sim", b"chi_tra", b""], True),
        ([b"eng", b"chi_sim", b"chi_tra", b"../private"], True),
        ([b"eng", b"chi_sim", b"chi_tra", b"eng"], True),
        ([b"eng", b"chi_sim", b"chi_tra", b"a" * 64], True),
        ([b"eng", b"chi_sim", b"chi_tra"] + [f"lang{i}".encode() for i in range(13)], False),
    ],
)
def test_tesseract_rejects_null_or_malformed_language_arrays_and_frees_owned_memory(
    tmp_path, monkeypatch, names, terminated
):
    _check_tesseract_language_failure(tmp_path, monkeypatch, names, terminated=terminated)


def _check_tesseract_language_failure(
    tmp_path, monkeypatch, names, *, kind="fast", terminated=True
):
    calls = []
    buffers, valid = _language_array([b"eng", b"chi_sim", b"chi_tra"])
    bad_buffers, invalid = (
        _language_array(names, terminated=terminated)
        if names is not None
        else ([], environment.ctypes.POINTER(environment.ctypes.c_void_p)())
    )
    current = None

    def initialize(handle, path, languages):
        nonlocal current
        current = Path(environment.os.fsdecode(path)).name
        assert languages == b"eng+chi_sim+chi_tra"
        calls.append((current, "init"))
        return 0

    library = SimpleNamespace(
        TessBaseAPICreate=lambda: 123,
        TessBaseAPIInit3=initialize,
        TessBaseAPIGetLoadedLanguagesAsVector=lambda handle: invalid if current == kind else valid,
        TessDeleteTextArray=lambda values: calls.append((current, "free")),
        TessBaseAPIEnd=lambda handle: calls.append((current, "end")),
        TessBaseAPIDelete=lambda handle: calls.append((current, "delete")),
    )
    monkeypatch.setattr(environment.ctypes, "CDLL", lambda path: library)
    monkeypatch.setattr(environment, "_command", lambda args: "tesseract 5.5.3\nleptonica-1.86.0\n")
    monkeypatch.setattr(environment, "_loaded_native", lambda: [])
    with rejected("candidate_initialization_failed"):
        environment._tesseract(tmp_path)
    expected = []
    for name in ("fast", "best"):
        expected.append((name, "init"))
        if name != kind or names is not None:
            expected.append((name, "free"))
        expected.extend([(name, "end"), (name, "delete")])
        if name == kind:
            break
    assert calls == expected
    assert buffers and (bad_buffers or names in (None, []))


@pytest.mark.parametrize("cleanup", ["free", "end"])
def test_tesseract_cleanup_exception_still_disposes_api(tmp_path, monkeypatch, cleanup):
    calls = []
    buffers, languages = _language_array([b"eng", b"chi_sim", b"chi_tra"])

    def dispose(name):
        calls.append(name)
        if name == cleanup:
            raise RuntimeError("fictional cleanup failure")

    library = SimpleNamespace(
        TessBaseAPICreate=lambda: 123,
        TessBaseAPIInit3=lambda *args: 0,
        TessBaseAPIGetLoadedLanguagesAsVector=lambda handle: languages,
        TessDeleteTextArray=lambda values: dispose("free"),
        TessBaseAPIEnd=lambda handle: dispose("end"),
        TessBaseAPIDelete=lambda handle: calls.append("delete"),
    )
    monkeypatch.setattr(environment.ctypes, "CDLL", lambda path: library)
    monkeypatch.setattr(environment, "_command", lambda args: "tesseract 5.5.3\nleptonica-1.86.0\n")
    monkeypatch.setattr(environment, "_loaded_native", lambda: [])
    with pytest.raises(RuntimeError, match="fictional cleanup failure"):
        environment._tesseract(tmp_path)
    assert buffers and calls == ["free", "end", "delete"]


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

    def native(paths, *, mapped_paths):
        observed_roots.extend(paths)
        assert inactive not in mapped_paths
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


@pytest.mark.parametrize(
    "defect",
    [
        None,
        "legacy",
        "latin1",
        "missing_hash",
        "missing_size",
        "missing_path",
        "missing_file",
        "corrupt",
    ],
)
def test_embedded_python_source_notices_require_real_files_and_exact_original_bytes(
    tmp_path, monkeypatch, defect
):
    main, embedded = tmp_path / "LICENSE.txt", tmp_path / "python-vendor-LICENSE"
    main.write_bytes(b"Fictional main license\n")
    embedded.write_bytes(b"Original notice\n" if defect != "latin1" else b"Original \xe9\n")
    notice = {"path": str(embedded), **environment._identity(embedded)}
    inventory = {
        "version": 1,
        "packages": [],
        "common_licenses": [],
        "python_license": {"path": str(main), **environment._identity(main)},
        "python_embedded_notices": [notice],
    }
    if defect == "legacy":
        del inventory["python_embedded_notices"]
    elif defect in ("missing_hash", "missing_size", "missing_path"):
        del notice[
            {"missing_hash": "sha256", "missing_size": "byte_size", "missing_path": "path"}[defect]
        ]
    elif defect == "missing_file":
        embedded.unlink()
    elif defect == "corrupt":
        embedded.write_bytes(b"Tampered bytes\n")
    path = tmp_path / "inventory.json"
    path.write_text(json.dumps(inventory), encoding="utf8")
    monkeypatch.setattr(environment, "SYSTEM_PATH", path)
    if defect in (None, "legacy", "latin1"):
        verified = environment._system_inventory()
        if defect != "legacy":
            observed = verified["python_embedded_notices"][0]
            assert observed["text"].encode(observed["encoding"]) == embedded.read_bytes()
            report = {"system": verified, "installed": [], "source_assets": {"files": []}}
            assert ("source", None, None, embedded.name, notice["sha256"]) in environment._evidence(
                report
            )
    else:
        with rejected("candidate_inventory_invalid"):
            environment._system_inventory()


@pytest.mark.parametrize("inherited", [None, "/inherited"])
def test_ldd_recovers_only_actual_mapped_loader_context_without_mutating_runtime(
    monkeypatch, inherited
):
    files, calls = _fictional_native_context(monkeypatch, inherited)
    roots = [environment.Path("/mapped/root.so"), environment.Path("/vendored/loaded.so")]
    before = dict(environment.os.environ)
    records = environment._native_inventory(roots, mapped_paths=roots)
    assert dict(environment.os.environ) == before
    assert {item["path"] for item in records} == {
        "/mapped/root.so",
        "/vendored/loaded.so",
        "/vendored/private.so",
        "/system/runtime.so",
    }
    parent = next(item for item in records if item["path"] == "/mapped/root.so")
    assert "not found" in parent["ldd_default"] and "not found" not in parent["ldd"]
    assert parent["dependencies"] == ["/vendored/private.so"]
    assert all(not item["missing"] for item in records)
    for item in records:
        data = files[item["path"]]
        assert item["byte_size"] == len(data)
        assert item["sha256"] == hashlib.sha256(data).hexdigest()
        assert item["ldd_search_context"] == {
            "inherited_ld_library_path": inherited,
            "mapped_directories": ["/mapped", "/vendored"],
            "ld_library_path": (inherited + ":" if inherited else "") + "/mapped:/vendored",
        }
    assert all("/unused" not in call["env"].get("LD_LIBRARY_PATH", "") for call in calls)
    assert all(set(call["env"]) <= {"LC_ALL", "LD_LIBRARY_PATH"} for call in calls)
    assert calls and all(call["env"]["LC_ALL"] == "C" for call in calls)


@pytest.mark.parametrize("defect", ["not_found", "nonexistent", "non_elf"])
def test_ldd_context_cannot_hide_unresolved_or_invalid_dependency_files(monkeypatch, defect):
    _fictional_native_context(monkeypatch, None, defect)
    roots = [environment.Path("/mapped/root.so"), environment.Path("/vendored/loaded.so")]
    records = environment._native_inventory(roots, mapped_paths=roots)
    parent = next(item for item in records if item["path"] == "/mapped/root.so")
    assert parent["missing"] is True
    if defect == "not_found":
        assert "not found" in parent["ldd"]
    else:
        assert parent["dependencies"] == ["/vendored/private.so"]
        assert "/vendored/private.so" not in {item["path"] for item in records}


def test_loaded_mapping_names_serialize_in_string_order_with_nested_and_dot_directories(
    monkeypatch,
):
    nested = "/fictional/numpy/_core/module.so"
    dotted = "/fictional/numpy.libs/runtime.so"

    class MappingPath(PurePosixPath):
        def resolve(self):
            return self

        def read_text(self):
            assert str(self) == "/proc/self/maps"
            return "\n".join(f"0 0 0 0 0 {path}" for path in (nested, dotted, nested))

    monkeypatch.setattr(environment, "Path", MappingPath)
    monkeypatch.setattr(environment, "_elf", lambda path: str(path) in (nested, dotted))
    assert [str(path) for path in environment._loaded_native()] == [dotted, nested]


def _fictional_native_context(monkeypatch, inherited, defect=None):
    class AuditPath(PurePosixPath):
        def resolve(self):
            return self

    files = {
        path: b"\x7fELF" + path.encode()
        for path in (
            "/mapped/root.so",
            "/vendored/loaded.so",
            "/vendored/private.so",
            "/system/runtime.so",
            "/unused/not_loaded.so",
        )
    }
    if defect == "nonexistent":
        del files["/vendored/private.so"]
    elif defect == "non_elf":
        files["/vendored/private.so"] = b"This fictional file is not ELF."
    calls = []

    def run(arguments, **kwargs):
        assert arguments[0] == "ldd"
        calls.append(kwargs)
        paths = kwargs["env"].get("LD_LIBRARY_PATH", "").split(":")
        if arguments[1] == "/mapped/root.so":
            output = (
                "private.so => /vendored/private.so (0x1)\n"
                if "/vendored" in paths and defect != "not_found"
                else "private.so => not found\n"
            )
        elif arguments[1] == "/vendored/private.so":
            output = "runtime.so => /system/runtime.so (0x2)\n"
        else:
            output = "statically linked\n"
        return SimpleNamespace(stdout=output.encode(), stderr=b"", returncode=0)

    monkeypatch.setattr(environment, "Path", AuditPath)
    monkeypatch.setattr(
        environment, "_elf", lambda path: files.get(str(path), b"").startswith(b"\x7fELF")
    )
    monkeypatch.setattr(
        environment,
        "_identity",
        lambda path: {
            "byte_size": len(files[str(path)]),
            "sha256": hashlib.sha256(files[str(path)]).hexdigest(),
        },
    )
    monkeypatch.setattr(environment.subprocess, "run", run)
    monkeypatch.setenv("FICTIONAL_DO_NOT_INHERIT", "secret")
    if inherited is None:
        monkeypatch.delenv("LD_LIBRARY_PATH", raising=False)
    else:
        monkeypatch.setenv("LD_LIBRARY_PATH", inherited)
    return files, calls
