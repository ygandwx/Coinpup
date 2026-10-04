"""Fictional files exercise real hashing/offline verifiers without SDKs or recognition."""

import json
from copy import deepcopy
from types import SimpleNamespace

import pytest

from scripts.ocr_benchmark import formal_freeze as freeze


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf8", newline="\n")


def asset(path, name):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(("fictional bytes: " + name).encode())
    return {"relative_path": name, **freeze._identity(path)}


@pytest.fixture
def fixture(tmp_path, monkeypatch):
    root, corpus, assets, development = [
        tmp_path / name for name in ("root", "corpus", "assets", "dev")
    ]
    for directory in (*freeze.SOURCE_DIRS, "tests/fixtures/ocr", "ops/ocr-benchmark"):
        (root / directory).mkdir(parents=True)
    for name in ("formal_freeze.py", "formal_run.py", "formal_report.py", "phase_clock.py"):
        (root / freeze.SOURCE_DIRS[1] / name).write_bytes(b"# fictional source\n")
    (root / freeze.SOURCE_DIRS[0] / "__init__.py").write_bytes(b"# fictional source\n")
    for name in (
        freeze.PROTOCOL_PATH,
        "pyproject.toml",
        "requirements.lock",
        "ops/ocr-benchmark/Dockerfile",
    ):
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"fictional frozen input\n")
    records, cases, dev_cases = [], [], []
    for group, count in freeze.CASE_COUNTS.items():
        for number in range(count):
            name = f"{group}-{number:02d}.bin"
            record = asset(corpus / name, name)
            records.append(record)
            cases.append({"id": f"{group}-{number:02d}", "group": group, **record})
    for number in range(1, 5):
        name = f"D0{number}-scan.pdf"
        record = asset(corpus / name, name)
        records.append(record)
        dev_cases.append(
            {
                "id": f"D0{number}-scan",
                "template_id": f"D0{number}",
                "group": "development",
                "carrier": "scan_pdf",
                "reference_text": "X",
                "expected_fields": {"header.total": "1.00"},
                "raster_sha256": [record["sha256"]],
                **record,
            }
        )
    for number in range(2):
        records.append(asset(corpus / f"font-{number}.ttf", f"font-{number}.ttf"))
    manifest = {"version": 1, "cases": cases, "development": dev_cases, "assets": records}
    save(corpus / "manifest.json", manifest)
    save(
        root / "tests/fixtures/ocr/corpus-frozen.json",
        {
            "version": 1,
            "platform": "linux-x86_64",
            "assets": records,
            "manifest_sha256": freeze._identity(corpus / "manifest.json")["sha256"],
        },
    )
    model_records = []
    for number in range(53):
        name = f"fictional-{number:02d}.bin"
        record = asset(assets / name, name)
        model_records.append(
            {
                "name": name,
                "url": "https://raw.githubusercontent.com/fictional/test/main/" + name,
                **{key: record[key] for key in ("sha256", "byte_size")},
            }
        )
    engine_manifest = root / "tests/fixtures/ocr/engine-assets.json"
    save(engine_manifest, {"version": 1, "files": model_records})
    monkeypatch.setattr(freeze.engine_assets, "ASSETS_PATH", engine_manifest)
    evidence_path = root / "tests/fixtures/ocr/development-results.json"
    reports = [
        {
            "profile": profile,
            "C": 4,
            "T": 4,
            "E": 0,
            "edits": 0,
            "reference_chars": 4,
            "stable_passes": 2,
        }
        for profile in freeze.PROFILES
    ]
    save(
        evidence_path,
        {
            "version": 1,
            "reports": reports,
            "selection": freeze.SELECTION,
            "source": {"kind": "fictional unit data"},
        },
    )
    save(development / "selection.json", freeze.SELECTION)
    config = freeze._build(root, evidence_path.relative_to(root).as_posix())
    save(root / freeze.CONFIG_PATH, config)
    return SimpleNamespace(
        root=root,
        corpus=corpus,
        assets=assets,
        selection=development / "selection.json",
        evidence=evidence_path,
        config=config,
        manifest=manifest,
    )


def verify(fixture):
    return freeze.load_and_verify(fixture.root, fixture.corpus, fixture.assets, fixture.selection)


def full_reports(fixture):
    for profile in freeze.PROFILES:
        output = {
            "status": "processed",
            "raw_text": "X",
            "timings_ns": {"prepare": 1},
            "parsed": {"fields": [{"path": "header.total", "value": "1.00", "status": "certain"}]},
        }
        metadata = {"profile": profile}
        if profile["engine"] == "tesseract":
            metadata.update(
                versions={"tesseract": "5.5.3"},
                loaded_languages=["eng", "chi_sim", "chi_tra"],
                parameters={
                    "languages": ["eng", "chi_sim", "chi_tra"],
                    "dpi": 300,
                    "level": "textline",
                },
            )
        else:
            metadata.update(
                versions={
                    "paddle": "3.4.0",
                    "paddlex": "3.7.0",
                    "paddleocr": "3.7.0",
                    "cv2": "4.10.0",
                },
                parameters={
                    "device": "cpu",
                    "cpu_threads": 1,
                    "enable_hpi": False,
                    "enable_mkldnn": False,
                    "precision": "fp32",
                    "models": ["PP-OCRv6_small_det", "PP-OCRv6_small_rec"],
                },
            )
        records = [
            {
                "case_id": case["id"],
                "pass": repeat,
                "output": {
                    **deepcopy(output),
                    "pages": [{"raster_rgb_sha256": case["raster_sha256"][0]}],
                },
            }
            for repeat in range(2)
            for case in fixture.manifest["development"]
        ]
        name = (
            "paddle"
            if profile["engine"] == "paddle"
            else f"tesseract-{profile['model_set']}-psm{profile['psm']}"
        )
        save(
            fixture.selection.with_name(name + ".json"),
            {
                "profile": profile,
                "metadata": metadata,
                "records": records,
                "C": 4,
                "T": 4,
                "E": 0,
                "edits": 0,
                "reference_chars": 4,
                "stable_passes": 2,
            },
        )


def test_real_offline_bytes_inventory_and_case_order(fixture):
    result = verify(fixture)
    assert result["profiles"]["tesseract"] == {"engine": "tesseract", "model_set": "fast", "psm": 6}
    assert {group: len(ids) for group, ids in result["case_ids"].items()} == freeze.CASE_COUNTS
    assert all(ids == sorted(ids) for ids in result["case_ids"].values())
    assert result["resources"]["memory_bytes"] == 4294967296
    assert result["schedule"]["paired_orders"] == ["AB", "BA", "AB", "BA", "AB"]


@pytest.mark.parametrize(
    "change",
    [
        "new_source",
        "changed_source",
        "deleted_source",
        "new_input",
        "resource",
        "bool_version",
        "schedule",
        "extra_key",
    ],
)
def test_unfrozen_source_or_configuration_cannot_refresh_itself(fixture, change):
    if change.endswith("source"):
        path = fixture.root / freeze.SOURCE_DIRS[0] / "__init__.py"
        if change == "new_source":
            path.with_name("unfrozen.py").write_bytes(b"# new code\n")
        elif change == "changed_source":
            path.write_bytes(b"# changed code\n")
        else:
            path.unlink()
    elif change == "new_input":
        (fixture.root / "ops/ocr-benchmark/unknown.lock").write_bytes(b"new input\n")
    else:
        config = deepcopy(fixture.config)
        if change == "resource":
            config["resources"]["swap_bytes"] = 1
        elif change == "bool_version":
            config["version"] = True
        elif change == "schedule":
            config["schedule"]["hot_repeats"] = 1
        else:
            config["trust_me"] = True
        save(fixture.root / freeze.CONFIG_PATH, config)
    before = (fixture.root / freeze.CONFIG_PATH).read_bytes()
    with pytest.raises(freeze.FormalFreezeError, match="formal_configuration_drift"):
        verify(fixture)
    assert (fixture.root / freeze.CONFIG_PATH).read_bytes() == before


@pytest.mark.parametrize("change", ["model", "corpus_file", "manifest"])
def test_real_offline_tampering_is_rejected(fixture, change):
    path = {
        "model": fixture.assets / "fictional-00.bin",
        "corpus_file": fixture.corpus / "text-00.bin",
        "manifest": fixture.corpus / "manifest.json",
    }[change]
    path.write_bytes(path.read_bytes() + b" ")
    with pytest.raises(freeze.FormalFreezeError):
        verify(fixture)


@pytest.mark.parametrize(
    "data", [b'{"version":1,"version":1}', b'{"x":NaN}', b'{"x":1e1000}', b"[]"]
)
def test_strict_bounded_json(tmp_path, monkeypatch, data):
    path = tmp_path / "strict.json"
    path.write_bytes(data)
    with pytest.raises(freeze.FormalFreezeError, match="formal_input_invalid"):
        freeze._json(path)
    monkeypatch.setattr(freeze, "MAX_BYTES", 3)
    path.write_bytes(b"large input")
    with pytest.raises(freeze.FormalFreezeError, match="formal_input_limit"):
        freeze._json(path)


def test_current_development_reports_recompute_counts_and_outputs(fixture):
    full_reports(fixture)
    assert verify(fixture)["case_ids"]
    report_path = fixture.selection.with_name("tesseract-fast-psm6.json")
    report = freeze._json(report_path)
    report["records"][4]["output"]["raw_text"] = "changed second pass"
    save(report_path, report)
    with pytest.raises(freeze.FormalFreezeError):
        verify(fixture)


@pytest.mark.parametrize(
    "change",
    ["wrong_language", "missing_report", "wrong_model", "wrong_raster", "falsified_counts"],
)
def test_current_development_evidence_is_not_summary_only(fixture, change):
    full_reports(fixture)
    path = fixture.selection.with_name("tesseract-fast-psm6.json")
    if change == "wrong_model":
        path = fixture.selection.with_name("paddle.json")
    report = freeze._json(path)
    if change == "missing_report":
        path.unlink()
    else:
        if change == "wrong_language":
            report["metadata"]["loaded_languages"] = ["eng"]
        elif change == "wrong_model":
            report["metadata"]["parameters"]["models"] = ["unfrozen"]
        elif change == "wrong_raster":
            report["records"][0]["output"]["pages"][0]["raster_rgb_sha256"] = freeze._identity(
                fixture.selection
            )["sha256"]
        else:
            report["C"] = 3
        save(path, report)
    with pytest.raises(freeze.FormalFreezeError, match="formal_development_invalid"):
        verify(fixture)


def test_freeze_requires_index_bytes_and_explicit_profile(fixture, monkeypatch):
    (fixture.root / freeze.CONFIG_PATH).unlink()
    monkeypatch.setattr(
        freeze.subprocess,
        "run",
        lambda argv, **kwargs: SimpleNamespace(
            returncode=0, stdout=(fixture.root / argv[-1][1:]).read_bytes()
        ),
    )
    config = freeze.freeze(fixture.root, fixture.evidence, selected_profile="fast:6")
    assert config == freeze._json(fixture.root / freeze.CONFIG_PATH)
    with pytest.raises(FileExistsError):
        freeze.freeze(fixture.root, fixture.evidence, selected_profile="fast:6")
    with pytest.raises(freeze.FormalFreezeError, match="formal_development_invalid"):
        freeze.freeze(fixture.root, fixture.evidence, selected_profile="best:6")
    monkeypatch.setattr(
        freeze.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout=b"different staged bytes"),
    )
    with pytest.raises(freeze.FormalFreezeError, match="formal_source_unstaged"):
        freeze.freeze(fixture.root, fixture.evidence, selected_profile="fast:6")


@pytest.fixture
def host_kernel(tmp_path, monkeypatch):
    proc = tmp_path / "proc"
    (proc / "sys/kernel/random").mkdir(parents=True)
    (proc / "cpuinfo").write_bytes(b"model name : Fictional CPU\n")
    (proc / "meminfo").write_bytes(b"MemTotal: 1234 kB\n")
    (proc / "sys/kernel/random/boot_id").write_bytes(b"fictional-boot-id\n")
    release = tmp_path / "os-release"
    release.write_bytes(b'NAME="Fictional Linux"\n')
    monkeypatch.setattr(freeze, "PROC_ROOT", proc)
    monkeypatch.setattr(freeze, "OS_RELEASE", release)
    monkeypatch.setattr(freeze.platform, "system", lambda: "Linux")
    monkeypatch.setattr(freeze.os, "sched_getaffinity", lambda pid: {4, 2}, raising=False)
    return proc, release


def test_host_snapshot_has_actual_affinity_and_no_hostname(host_kernel):
    proc, _ = host_kernel
    facts = freeze.host_facts()
    assert facts["affinity"] == [2, 4]
    assert facts["memory_total_bytes"] == 1234 * 1024
    assert facts["boot_id_sha256"] == freeze._identity(proc / "sys/kernel/random/boot_id")["sha256"]
    assert "hostname" not in facts and "boot_id" not in facts


def test_host_release_resolves_fixed_system_link_before_regular_read(host_kernel, monkeypatch):
    _, release = host_kernel
    calls = []

    def resolve(*, strict):
        calls.append(strict)
        return release

    # Windows need not grant symlink creation: emulate only the trusted path resolution.
    monkeypatch.setattr(freeze, "OS_RELEASE", SimpleNamespace(resolve=resolve))
    assert freeze.host_facts()["os_release"] == release.read_text(encoding="utf8")
    assert calls == [True]
