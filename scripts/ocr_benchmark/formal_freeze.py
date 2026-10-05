"""Freeze actual inputs once; verification never refreshes their identities."""

import argparse
import hashlib
import json
import os
import platform
import random
import re
import subprocess
from copy import deepcopy
from pathlib import Path, PurePosixPath

from . import engine_assets
from .corpus import CONFIG_PATH as RENDER_CONFIG_PATH
from .corpus import TRUTH_PATH, _reference, generate_corpus, verify_corpus
from .development_run import PROFILES, development_cases, select_tesseract, summarize_profile

CONFIG_PATH = "tests/fixtures/ocr/formal-config.json"
PROTOCOL_PATH = "docs/engineering/ocr-benchmark.md"
SOURCE_DIRS = ("services/api/src/coinpup_api/ocr", "scripts/ocr_benchmark")
PROC_ROOT, OS_RELEASE = Path("/proc"), Path("/etc/os-release")
MAX_BYTES = 64 * 1024**2
RESOURCES = {
    "cpu_count": 2,
    "memory_bytes": 4294967296,
    "swap_bytes": 0,
    "pids": 128,
    "threads": 1,
    "startup_seconds": 120,
    "page_seconds": 60,
    "sampling_ns": 10000000,
}
SCHEDULE = {
    "cold_repeats": 5,
    "cold_anchor": "D01",
    "warm_templates": ["D01", "D02"],
    "hot_repeats": 5,
    "paired_orders": ["AB", "BA", "AB", "BA", "AB"],
    "A": "tesseract",
    "B": "paddle",
    "case_order": "sorted_case_id",
}
CASE_COUNTS = {"text": 12, "ocr": 24, "degraded": 12, "error": 8}
SELECTION = {
    "version": 1,
    "tesseract": {"engine": "tesseract", "model_set": "fast", "psm": 6},
    "paddle": {"engine": "paddle"},
    "selection_rule": "accuracy_then_CER_then_fast_then_PSM_6_3_11",
    "formal_inference_performed": False,
}
COUNTERS = ("C", "T", "E", "edits", "reference_chars", "stable_passes")
CLEAR_SEED = 2026100501
CLEAR_CASES = {"text": 12, "ocr": 24, "degraded": 0, "error": 0}


def _sample_truth(truth, seed):
    if type(seed) is not int or seed != CLEAR_SEED:
        _fail("clear_seed_invalid")
    result, rng = deepcopy(truth), random.Random(seed)

    def money(raw, factor):
        if not re.fullmatch(r"[+-]?(?:[0-9]+|[0-9]{1,3}(?:,[0-9]{3})+)\.[0-9]{2}", raw):
            _fail("clear_truth_invalid")
        units = int(raw.replace(",", "").replace(".", "")) * factor
        whole, cents = divmod(abs(units), 100)
        width = len(raw.lstrip("+-").split(".")[0].replace(",", ""))
        digits = str(whole).zfill(width)
        whole = digits
        if "," in raw:
            groups = [digits[max(0, i - 3) : i] for i in range(len(digits), 0, -3)]
            whole = ",".join(reversed(groups))
        sign = "-" if raw.startswith("-") else "+" if raw.startswith("+") else ""
        return f"{sign}{whole}.{cents:02d}"

    for template in result["templates"]:
        factor, year, month = (
            rng.randrange(2, 5),
            rng.randrange(2035, 2045),
            rng.choice((1, 3, 5, 7)),
        )

        def dates(text, year=year, month=month):
            return re.sub(r"2026-09-([0-9]{2})", lambda m: f"{year}-{month:02d}-{m[1]}", text)

        for key, value in template["header"].items():
            template["header"][key] = (
                dates(value)
                if key == "document_date"
                else value
                if key == "currency"
                else money(value, factor)
            )
        for page in template["pages"]:
            page["lines"] = [
                re.sub(
                    r"[+-]?(?:[0-9]+|[0-9]{1,3}(?:,[0-9]{3})+)\.[0-9]{2}",
                    lambda m, factor=factor: money(m[0], factor),
                    dates(line),
                )
                for line in page["lines"]
            ]
            for row in page["rows"]:
                row.update(
                    date=dates(row["date"]),
                    amount=money(row["amount"], factor),
                    raw_amount=money(row["raw_amount"], factor),
                )
        template["pages"][0]["lines"][0] += f" / {seed}"
    result["sampling_seed"] = seed
    return result


def _clear_inputs(corpus_dir):
    return Path(corpus_dir).with_name(Path(corpus_dir).name + "-inputs")


def prepare_clear(source_dir, corpus_dir, *, seed=CLEAR_SEED):
    """Sample new truth before rendering; reuse the unchanged, deterministic v1 renderer."""
    inputs = _clear_inputs(corpus_dir)
    truth = _sample_truth(_json(TRUTH_PATH), seed)
    config = {**_json(RENDER_CONFIG_PATH), "degraded": [], "errors": []}
    inputs.mkdir(parents=True, exist_ok=False)
    for name, value in (("truth.json", truth), ("configuration.json", config)):
        with (inputs / name).open("x", encoding="utf8", newline="\n") as stream:
            stream.write(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n")
    return generate_corpus(
        source_dir,
        corpus_dir,
        truth_path=inputs / "truth.json",
        config_path=inputs / "configuration.json",
    )


def _clear_snapshot(root, corpus_dir, assets_dir, selection_path):
    root, corpus_dir, selection_path = Path(root), Path(corpus_dir), Path(selection_path)
    inputs = _clear_inputs(corpus_dir)
    truth, config = _json(inputs / "truth.json"), _json(inputs / "configuration.json")
    if truth != _sample_truth(_json(TRUTH_PATH), CLEAR_SEED) or config != {
        **_json(RENDER_CONFIG_PATH),
        "degraded": [],
        "errors": [],
    }:
        _fail("clear_truth_drift")
    engine_assets.verify_assets(assets_dir)
    manifest = verify_corpus(corpus_dir)
    groups = {
        group: sorted(case["id"] for case in manifest["cases"] if case["group"] == group)
        for group in CLEAR_CASES
    }
    if (
        len(manifest["cases"]) != 36
        or any(
            len(ids) != CLEAR_CASES[group] or len(set(ids)) != len(ids)
            for group, ids in groups.items()
        )
        or manifest["truth_sha256"] != _identity(inputs / "truth.json")["sha256"]
        or manifest["configuration_sha256"] != _identity(inputs / "configuration.json")["sha256"]
    ):
        _fail("formal_corpus_drift")
    templates = {template["id"]: template for template in truth["templates"]}
    expected_ids = {
        f"{name}-{carrier}" for name in templates for carrier in ("text", "scan", "photo")
    }
    if {case["id"] for case in manifest["cases"]} != expected_ids:
        _fail("formal_corpus_drift")
    for case in manifest["cases"]:
        template = templates[case["template_id"]]
        carrier = "photo" if case["carrier"] == "photo" else "scan"
        expected = _reference(template, template, carrier)
        if any(case.get(key) != value for key, value in expected.items()):
            _fail("formal_corpus_drift")
    evidence, report_files = [], []
    for profile in PROFILES:
        name = (
            "paddle"
            if profile["engine"] == "paddle"
            else f"tesseract-{profile['model_set']}-psm{profile['psm']}"
        )
        path = selection_path.parent / (name + ".json")
        report = _json(path)
        _metadata(report["metadata"], profile)
        evidence.append({"profile": profile, **{key: report[key] for key in COUNTERS}})
        report_files.append({"name": path.name, **_identity(path)})
    if (
        any(r["stable_passes"] != 2 for r in evidence)
        or select_tesseract(evidence) != SELECTION["tesseract"]
    ):
        _fail("formal_development_invalid")
    _verify_development(selection_path, {"reports": evidence}, manifest)
    return {
        "version": 2,
        "sampling_seed": CLEAR_SEED,
        "render_seed": manifest["seed"],
        "profiles": {engine: SELECTION[engine] for engine in ("tesseract", "paddle")},
        "resources": RESOURCES,
        "schedule": {
            **SCHEDULE,
            "cold_repeats": 3,
            "hot_repeats": 3,
            "paired_orders": ["AB", "BA", "AB"],
        },
        "cases": CLEAR_CASES,
        "case_ids": groups,
        "corpus_manifest_sha256": _identity(corpus_dir / "manifest.json")["sha256"],
        "generated_inputs": {
            name: _identity(inputs / name) for name in ("truth.json", "configuration.json")
        },
        "development_inputs": [
            *report_files,
            {"name": selection_path.name, **_identity(selection_path)},
        ],
        **_inventory(root),
    }


def freeze_clear(root, corpus_dir, assets_dir, selection_path, output_path):
    snapshot = _clear_snapshot(root, corpus_dir, assets_dir, selection_path)
    with Path(output_path).open("x", encoding="utf8", newline="\n") as stream:
        stream.write(json.dumps(snapshot, ensure_ascii=False, sort_keys=True, indent=2) + "\n")
    return snapshot


def load_clear(root, corpus_dir, assets_dir, selection_path, frozen_path):
    snapshot = _json(frozen_path)
    if _canonical(snapshot) != _canonical(
        _clear_snapshot(root, corpus_dir, assets_dir, selection_path)
    ):
        _fail("formal_configuration_drift")
    return snapshot


class FormalFreezeError(Exception):
    def __init__(self, reason):
        self.reason = reason
        super().__init__(reason)


def _fail(reason):
    raise FormalFreezeError(reason)


def _bytes(path, *, maximum=MAX_BYTES):
    if type(maximum) is not int or not 1 <= maximum <= MAX_BYTES:
        _fail("formal_input_invalid")
    if path.is_symlink() or path.is_junction() or not path.is_file():
        _fail("formal_input_invalid")
    with path.open("rb") as stream:
        data = stream.read(maximum + 1)
    if len(data) > maximum:
        _fail("formal_input_limit")
    return data


def _identity(path):
    data = _bytes(path)
    return {"sha256": hashlib.sha256(data).hexdigest(), "byte_size": len(data)}


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            _fail("formal_input_invalid")
        result[key] = value
    return result


def _constant(_):
    _fail("formal_input_invalid")


def _canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False)


def _json(path):
    try:
        value = json.loads(
            _bytes(path, maximum=MAX_BYTES), object_pairs_hook=_pairs, parse_constant=_constant
        )
        _canonical(value)  # Also reject overflowed JSON floats and invalid recursive structures.
        if type(value) is not dict:
            _fail("formal_input_invalid")
        return value
    except (ValueError, UnicodeError, RecursionError):
        _fail("formal_input_invalid")


def _path(root, name):
    relative = PurePosixPath(name)
    if (
        relative.is_absolute()
        or ".." in relative.parts
        or relative.as_posix() != name
        or "\\" in name
    ):
        _fail("formal_input_invalid")
    target = root / relative
    if not target.resolve().is_relative_to(root.resolve()):
        _fail("formal_input_invalid")
    if any(part.is_symlink() or part.is_junction() for part in (target, *target.parents)):
        _fail("formal_input_invalid")
    return target


def _inventory(root):
    sources, inputs = [], [PROTOCOL_PATH, "pyproject.toml"]
    for directory in SOURCE_DIRS:
        for path in (root / directory).rglob("*"):
            if path.is_symlink() or path.is_junction():
                _fail("formal_input_invalid")
            if path.is_file() and path.suffix == ".py":
                sources.append(path.relative_to(root).as_posix())
    for pattern in ("requirements*.lock", "tests/fixtures/ocr/*.json", "ops/ocr-benchmark/*"):
        inputs.extend(
            path.relative_to(root).as_posix() for path in root.glob(pattern) if path.is_file()
        )
    inputs = sorted(set(inputs) - {CONFIG_PATH})
    if not {
        "scripts/ocr_benchmark/" + name
        for name in ("formal_run.py", "formal_report.py", "phase_clock.py")
    } <= set(sources):
        _fail("formal_source_inventory_incomplete")
    return {
        "source_inventory": {name: _identity(_path(root, name)) for name in sorted(sources)},
        "inputs": {name: _identity(_path(root, name)) for name in inputs},
    }


def _evidence(root, name):
    evidence = _json(_path(root, name))
    if type(evidence.get("version")) is not int or evidence["version"] != 1:
        _fail("formal_development_invalid")
    reports = evidence.get("reports")
    if type(reports) is not list or len(reports) != 7 or not evidence.get("source"):
        _fail("formal_development_invalid")
    profiles = []
    for report in reports:
        if type(report) is not dict or any(
            type(report.get(key)) is not int or report[key] < 0 for key in COUNTERS
        ):
            _fail("formal_development_invalid")
        if (
            report["stable_passes"] != 2
            or report["C"] > report["T"]
            or report["T"] <= 0
            or report["reference_chars"] <= 0
        ):
            _fail("formal_development_invalid")
        profiles.append(_canonical(report.get("profile")))
    if sorted(profiles) != sorted(_canonical(profile) for profile in PROFILES):
        _fail("formal_development_invalid")
    selected = select_tesseract(reports)
    if _canonical(selected) != _canonical(SELECTION["tesseract"]) or _canonical(
        evidence.get("selection")
    ) != _canonical(SELECTION):
        _fail("formal_development_invalid")
    return evidence


def _metadata(metadata, profile):
    if type(metadata) is not dict or _canonical(metadata.get("profile")) != _canonical(profile):
        _fail("formal_development_invalid")
    if profile["engine"] == "tesseract":
        if (
            sorted(metadata.get("loaded_languages", [])) != ["chi_sim", "chi_tra", "eng"]
            or metadata.get("versions") != {"tesseract": "5.5.3"}
            or metadata.get("parameters")
            != {"languages": ["eng", "chi_sim", "chi_tra"], "dpi": 300, "level": "textline"}
        ):
            _fail("formal_development_invalid")
    else:
        parameters = metadata.get("parameters", {})
        if metadata.get("versions") != {
            "paddle": "3.4.0",
            "paddlex": "3.7.0",
            "paddleocr": "3.7.0",
            "cv2": "4.10.0",
        } or any(
            _canonical(parameters.get(key)) != _canonical(value)
            for key, value in {
                "device": "cpu",
                "cpu_threads": 1,
                "enable_hpi": False,
                "enable_mkldnn": False,
                "precision": "fp32",
                "models": ["PP-OCRv6_small_det", "PP-OCRv6_small_rec"],
            }.items()
        ):
            _fail("formal_development_invalid")


def _verify_development(selection_path, evidence, manifest):
    if _canonical(_json(selection_path)) != _canonical(SELECTION):
        _fail("formal_development_invalid")
    reports = []
    for profile in PROFILES:
        name = (
            "paddle"
            if profile["engine"] == "paddle"
            else f"tesseract-{profile['model_set']}-psm{profile['psm']}"
        )
        reports.append((selection_path.parent / (name + ".json"), profile))
    present = [path.exists() for path, _ in reports]
    if not any(present):
        return
    if not all(present):
        _fail("formal_development_invalid")
    expected_by_profile = {_canonical(r["profile"]): r for r in evidence["reports"]}
    cases = development_cases(manifest)
    for path, profile in reports:
        report = _json(path)
        if _canonical(report.get("profile")) != _canonical(profile):
            _fail("formal_development_invalid")
        _metadata(report.get("metadata"), profile)
        expected = expected_by_profile[_canonical(profile)]
        if any(_canonical(report.get(key)) != _canonical(expected[key]) for key in COUNTERS):
            _fail("formal_development_invalid")
        records = report.get("records")
        if type(records) is not list or len(records) != 8:
            _fail("formal_development_invalid")
        passes = []
        for repeat in range(2):
            batch = records[repeat * 4 : (repeat + 1) * 4]
            if [row.get("case_id") for row in batch] != [case["id"] for case in cases] or any(
                type(row.get("pass")) is not int or row["pass"] != repeat for row in batch
            ):
                _fail("formal_development_invalid")
            passes.append([row["output"] for row in batch])
            for case, row in zip(cases, batch, strict=True):
                if (
                    row["output"]["status"] == "processed"
                    and [page["raster_rgb_sha256"] for page in row["output"]["pages"]]
                    != case["raster_sha256"]
                ):
                    _fail("formal_development_invalid")
        calculated = summarize_profile(profile, cases, passes)
        if any(calculated[key] != expected[key] for key in COUNTERS):
            _fail("formal_development_invalid")


def _build(root, evidence_name):
    _evidence(root, evidence_name)
    frozen = _json(_path(root, "tests/fixtures/ocr/corpus-frozen.json"))
    asset_manifest = _json(_path(root, "tests/fixtures/ocr/engine-assets.json"))
    if (
        frozen.get("platform") != "linux-x86_64"
        or len(frozen.get("assets", [])) != 62
        or len(asset_manifest.get("files", [])) != 53
        or not re.fullmatch(r"[0-9a-f]{64}", frozen.get("manifest_sha256", ""))
    ):
        _fail("formal_input_invalid")
    return {
        "version": 1,
        "profiles": {engine: SELECTION[engine] for engine in ("tesseract", "paddle")},
        "resources": RESOURCES,
        "schedule": SCHEDULE,
        "cases": CASE_COUNTS,
        "development_evidence": evidence_name,
        "corpus_manifest_sha256": frozen["manifest_sha256"],
        **_inventory(root),
    }


def load_and_verify(root, corpus_dir, assets_dir, selection_path):
    """Verify frozen repository bytes and real offline inputs before any formal inference."""
    try:
        root, corpus_dir, selection_path = Path(root), Path(corpus_dir), Path(selection_path)
        config = _json(_path(root, CONFIG_PATH))
        expected = _build(root, config["development_evidence"])
        if _canonical(config) != _canonical(expected):
            _fail("formal_configuration_drift")
        engine_assets.verify_assets(assets_dir)
        _json(corpus_dir / "manifest.json")
        if _identity(corpus_dir / "manifest.json")["sha256"] != config["corpus_manifest_sha256"]:
            _fail("formal_corpus_drift")
        manifest = verify_corpus(corpus_dir)
        groups = {
            group: sorted(case["id"] for case in manifest["cases"] if case["group"] == group)
            for group in CASE_COUNTS
        }
        if (
            len(manifest["cases"]) != 56
            or len({case["id"] for case in manifest["cases"]}) != 56
            or any(
                len(ids) != CASE_COUNTS[group] or len(set(ids)) != len(ids)
                for group, ids in groups.items()
            )
        ):
            _fail("formal_corpus_drift")
        _verify_development(
            selection_path, _evidence(root, config["development_evidence"]), manifest
        )
        return {**config, "case_ids": groups}
    except FormalFreezeError:
        raise
    except Exception:
        _fail("formal_verification_failed")


def freeze(root, evidence_path, *, selected_profile):
    """Explicitly publish a snapshot from canonical, already staged repository bytes."""
    root, evidence_path = Path(root).resolve(), Path(evidence_path).resolve()
    if selected_profile != "fast:6" or not evidence_path.is_relative_to(root):
        _fail("formal_development_invalid")
    config = _build(root, evidence_path.relative_to(root).as_posix())
    for name in [*config["source_inventory"], *config["inputs"]]:
        result = subprocess.run(
            ["git", "-c", f"safe.directory={root}", "-C", str(root), "show", ":" + name],
            capture_output=True,
            timeout=10,
            check=False,
        )
        identity = {
            "sha256": hashlib.sha256(result.stdout).hexdigest(),
            "byte_size": len(result.stdout),
        }
        if (
            result.returncode
            or result.stdout != _bytes(_path(root, name))
            or identity != (config["source_inventory"] | config["inputs"])[name]
        ):
            _fail("formal_source_unstaged")
    target = _path(root, CONFIG_PATH)
    with target.open("x", encoding="utf8", newline="\n") as output:
        output.write(json.dumps(config, ensure_ascii=False, sort_keys=True, indent=2) + "\n")
    return config


def host_facts():
    """Actual host identity without hostname or a raw boot identifier."""
    try:
        if platform.system() != "Linux":
            _fail("formal_host_unavailable")
        cpu = _bytes(PROC_ROOT / "cpuinfo").decode("utf8")
        memory = _bytes(PROC_ROOT / "meminfo").decode("ascii")
        total = re.findall(r"^MemTotal:\s+([0-9]+) kB$", memory, flags=re.MULTILINE)
        models = sorted(set(re.findall(r"^model name\s*:\s*(.+)$", cpu, flags=re.MULTILINE)))
        if len(total) != 1 or not models:
            _fail("formal_host_unavailable")
        return {
            "cpu_models": models,
            "logical_cpu_count": os.cpu_count(),
            "affinity": sorted(os.sched_getaffinity(0)),
            "memory_total_bytes": int(total[0]) * 1024,
            "os_release": _bytes(OS_RELEASE.resolve(strict=True)).decode("utf8"),
            "kernel_release": platform.release(),
            "kernel_version": platform.version(),
            "machine": platform.machine(),
            "python": platform.python_version(),
            "boot_id_sha256": hashlib.sha256(
                _bytes(PROC_ROOT / "sys/kernel/random/boot_id", maximum=128)
            ).hexdigest(),
        }
    except FormalFreezeError:
        raise
    except Exception:
        _fail("formal_host_unavailable")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("freeze", "verify", "prepare-clear", "freeze-clear"))
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--development-evidence", type=Path)
    parser.add_argument("--tesseract-profile", dest="selected_profile")
    parser.add_argument("--corpus-dir", type=Path)
    parser.add_argument("--assets-dir", type=Path)
    parser.add_argument("--selection", type=Path)
    parser.add_argument("--source-dir", type=Path)
    parser.add_argument("--output", type=Path)
    options = parser.parse_args()
    try:
        if options.command == "prepare-clear":
            if options.source_dir is None or options.corpus_dir is None:
                _fail("formal_configuration_invalid")
            result = prepare_clear(options.source_dir, options.corpus_dir)
            print(
                json.dumps(
                    {
                        "sampling_seed": CLEAR_SEED,
                        "render_seed": result["seed"],
                        "cases": len(result["cases"]),
                    }
                )
            )
            return
        if options.command == "freeze-clear":
            if any(
                value is None
                for value in (
                    options.corpus_dir,
                    options.assets_dir,
                    options.selection,
                    options.output,
                )
            ):
                _fail("formal_configuration_invalid")
            result = freeze_clear(
                options.root,
                options.corpus_dir,
                options.assets_dir,
                options.selection,
                options.output,
            )
            print(
                json.dumps(
                    {
                        "sampling_seed": result["sampling_seed"],
                        "config_sha256": _identity(options.output)["sha256"],
                        "cases": result["cases"],
                    }
                )
            )
            return
        if options.command == "freeze":
            if options.development_evidence is None or options.selected_profile is None:
                _fail("formal_configuration_invalid")
            result = freeze(
                options.root,
                options.development_evidence,
                selected_profile=options.selected_profile,
            )
        else:
            if any(
                value is None
                for value in (options.corpus_dir, options.assets_dir, options.selection)
            ):
                _fail("formal_configuration_invalid")
            result = load_and_verify(
                options.root, options.corpus_dir, options.assets_dir, options.selection
            )
        print(
            json.dumps(
                {
                    "version": 1,
                    "profiles": result["profiles"],
                    "sources": len(result["source_inventory"]),
                }
            )
        )
    except Exception as error:
        parser.exit(
            1,
            (error.reason if isinstance(error, FormalFreezeError) else "formal_freeze_failed")
            + "\n",
        )


if __name__ == "__main__":
    main()
