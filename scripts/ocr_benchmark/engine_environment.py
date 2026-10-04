"""Offline candidate initialization and byte-bound provenance, never document inference."""

import argparse
import contextlib
import ctypes
import hashlib
import importlib
import importlib.metadata
import json
import os
import platform
import re
import subprocess
import sys
from pathlib import Path, PurePosixPath

from scripts.ocr_benchmark import engine_assets

ROOT = Path(__file__).resolve().parents[2]
PROVENANCE_PATH = ROOT / "ops/ocr-benchmark/paddle-provenance.json"
POLICY_PATHS = (
    ROOT / "ops/ocr-benchmark/native-wheels-policy.json",
    ROOT / "ops/ocr-benchmark/native-system-policy.json",
)
SYSTEM_PATH = Path("/opt/environment-system/packages.json")
THREADS = ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS", "NUMEXPR_NUM_THREADS")
VERSIONS = {
    "paddleocr": "3.7.0",
    "paddlex": "3.7.0",
    "paddlepaddle": "3.4.0",
    "opencv-contrib-python": "4.10.0.84",
}


class CandidateEnvironmentError(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _fail(code):
    raise CandidateEnvironmentError(code) from None


def _json(path):
    try:
        if path.stat().st_size > 32 * 1024**2:
            _fail("candidate_inventory_invalid")
        value = json.loads(path.read_text(encoding="utf8"))
        if type(value) is not dict or value.get("version") != 1:
            _fail("candidate_inventory_invalid")
        return value
    except (OSError, ValueError, TypeError):
        _fail("candidate_inventory_invalid")


def _identity(path):
    digest, size = hashlib.sha256(), 0
    with path.open("rb") as stream:
        while chunk := stream.read(65536):
            size += len(chunk)
            digest.update(chunk)
    return {"byte_size": size, "sha256": digest.hexdigest()}


def _notice_text(path):
    data = path.read_bytes()
    try:
        return {"encoding": "utf-8", "text": data.decode("utf8")}
    except UnicodeDecodeError:
        return {"encoding": "latin-1", "text": data.decode("latin-1")}


def _command(arguments):
    try:
        result = subprocess.run(arguments, capture_output=True, timeout=120, check=False)
        output = result.stdout + result.stderr
        if len(output) > 1024**2 or result.returncode:
            _fail("candidate_command_failed")
        return output.decode("utf8")
    except (OSError, ValueError, subprocess.TimeoutExpired):
        _fail("candidate_command_failed")


def _models(directory, engine):
    try:
        manifest = engine_assets.verify_assets(directory)
    except engine_assets.EngineAssetError:
        _fail("candidate_models_invalid")
    selected = [
        item
        for item in manifest["files"]
        if item["role"] == ("model" if engine == "paddle" else "traineddata")
    ]
    if len(selected) != (10 if engine == "paddle" else 6):
        _fail("candidate_models_invalid")
    return [{key: item[key] for key in ("name", "sha256", "byte_size")} for item in selected]


def _runtime():
    if sys.platform != "linux" or platform.machine() != "x86_64":
        _fail("candidate_platform_unavailable")
    interfaces = sorted(
        line.split(":", 1)[0].strip()
        for line in Path("/proc/net/dev").read_text().splitlines()
        if ":" in line
    )
    if interfaces != ["lo"]:
        _fail("candidate_network_enabled")
    controls = {
        name: Path(f"/sys/fs/cgroup/{name}").read_text().strip()
        for name in ("memory.max", "memory.swap.max", "pids.max")
    }
    affinity = sorted(os.sched_getaffinity(0))
    if (
        controls != {"memory.max": "4294967296", "memory.swap.max": "0", "pids.max": "128"}
        or len(affinity) != 2
    ):
        _fail("candidate_resource_invalid")
    return {
        "python": platform.python_version(),
        "machine": platform.machine(),
        "interfaces": interfaces,
        "affinity": affinity,
        "cgroup": controls,
    }


def _agpl(value):
    return type(value) is str and bool(re.search(r"\bAGPL\b", value, flags=re.I))


def _allowed(policy):
    identifiers = policy.get("license_ids", [])
    return (
        policy.get("status") == "allowed"
        and bool(identifiers)
        and all(type(value) is str and value and not _agpl(value) for value in identifiers)
    )


def _installed(provenance):
    def normalize(value):
        return re.sub(r"[-_.]+", "-", value).lower()

    records = {normalize(record["package"]): record for record in provenance["records"]}
    result, natives = [], []
    problems = {"integrity": [], "license": [], "forbidden": []}
    for distribution in sorted(
        importlib.metadata.distributions(), key=lambda item: normalize(item.metadata["Name"])
    ):
        name, version = distribution.metadata["Name"], distribution.version
        source = records.get(normalize(name))
        record = {"package": name, "version": version, "metadata": None, "notices": []}
        if source is None or source["version"] != version:
            problems["integrity"].append(name)
        else:
            record["wheel_sha256"] = source["sha256"]
            for kind, items in (
                ("metadata", [source["metadata"]]),
                ("wheel", [source["wheel"]]),
                ("notices", source["notices"]),
            ):
                for item in items:
                    member = PurePosixPath(item["name"])
                    if member.is_absolute() or ".." in member.parts:
                        _fail("candidate_inventory_invalid")
                    path = Path(distribution.locate_file(item["name"]))
                    observed = {"name": item["name"], **_identity(path)}
                    if any(observed[key] != item[key] for key in ("sha256", "byte_size")):
                        problems["integrity"].append(name)
                    observed.update(_notice_text(path))
                    if kind in ("metadata", "wheel"):
                        record[kind] = observed
                    else:
                        record[kind].append(observed)
            record["source_notices"] = source.get("source_notices", [])
            for notice in record["source_notices"]:
                data = notice["text"].encode(notice["encoding"])
                if (
                    len(data) != notice["byte_size"]
                    or hashlib.sha256(data).hexdigest() != notice["sha256"]
                ):
                    problems["integrity"].append(name)
            policy = source.get("reviewed_license_policy", {})
            notice_names = {
                item["name"] for item in [*source["notices"], *record["source_notices"]]
            }
            if (
                not _allowed(policy)
                or not policy.get("evidence")
                or not set(policy["evidence"]).issubset(notice_names)
            ):
                problems["license"].append(name)
            if policy.get("status") == "rejected" or any(
                _agpl(value) for value in policy.get("license_ids", [])
            ):
                problems["forbidden"].append(name)
            record["reviewed_license_policy"] = policy
        for member in distribution.files or []:
            path = Path(distribution.locate_file(member)).resolve()
            if path.is_file() and re.search(
                r"\.(?:so(?:\.[A-Za-z0-9_.-]+)?|dylib|dll)$", str(path)
            ):
                natives.append(path)
        result.append(record)
    return result, natives, {key: sorted(set(value)) for key, value in problems.items()}


def _elf(path):
    try:
        with path.open("rb") as stream:
            return stream.read(4) == b"\x7fELF"
    except OSError:
        return False


def _native_inventory(paths):
    pending, records = list(paths), {}
    while pending:
        path = pending.pop().resolve()
        if str(path) in records or not _elf(path):
            continue
        output = _command(["ldd", str(path)])
        dependencies = sorted(
            {
                str(Path(item).resolve())
                for item in re.findall(r"(?:=>\s*)?(/[^\s()]+)\s*\(", output)
            }
        )
        record = {
            "path": str(path),
            **_identity(path),
            "ldd": output,
            "dependencies": dependencies,
            "missing": "not found" in output,
        }
        records[str(path)] = record
        pending.extend(Path(item) for item in dependencies)
    return sorted(records.values(), key=lambda item: item["path"])


def _loaded_native():
    paths = []
    for line in Path("/proc/self/maps").read_text().splitlines():
        parts = line.split(maxsplit=5)
        if len(parts) == 6 and parts[5].startswith("/"):
            path = Path(parts[5])
            if _elf(path):
                paths.append(path.resolve())
    return sorted(set(paths))


def _system_inventory():
    inventory = _json(SYSTEM_PATH)
    for package in inventory["packages"]:
        notice = package.get("copyright")
        if notice:
            path = Path(notice["path"])
            if _identity(path) != {key: notice[key] for key in ("sha256", "byte_size")}:
                _fail("candidate_inventory_invalid")
            notice.update(_notice_text(path))
    for notice in [inventory["python_license"], *inventory["common_licenses"]]:
        path = Path(notice["path"])
        if _identity(path) != {key: notice[key] for key in ("sha256", "byte_size")}:
            _fail("candidate_inventory_invalid")
        notice.update(_notice_text(path))
    return inventory


def _evidence(report):
    observed = set()
    for distribution in report["installed"]:
        for notice in [*distribution["notices"], *distribution.get("source_notices", [])]:
            observed.add(
                (
                    "wheel",
                    distribution["package"].lower(),
                    distribution["version"],
                    notice["name"],
                    notice["sha256"],
                )
            )
    for package in report["system"]["packages"]:
        notice = package.get("copyright")
        if notice:
            observed.add(
                ("system", package["name"], package["version"], notice["path"], notice["sha256"])
            )
    for notice in [
        report["system"]["python_license"],
        *report["system"]["common_licenses"],
        *report.get("build_notices", []),
    ]:
        observed.add(("source", None, None, Path(notice["path"]).name, notice["sha256"]))
    for item in [
        *report["source_assets"]["files"],
        *report["source_assets"].get("embedded_notices", []),
    ]:
        observed.add(("asset", None, None, item["name"], item["sha256"]))
    return observed


def _native_policy(records, policy, observed):
    bindings = {(item["sha256"], item["byte_size"]): item for item in policy["records"]}
    missing = []
    for record in records:
        binding = bindings.get((record["sha256"], record["byte_size"]))
        references = binding.get("evidence", []) if binding else []
        valid = bool(references) and all(
            (
                reference.get("kind"),
                (
                    reference.get("package", "").lower()
                    if reference.get("kind") == "wheel"
                    else reference.get("package")
                ),
                reference.get("version"),
                reference.get("name"),
                reference.get("sha256"),
            )
            in observed
            for reference in references
        )
        if binding is None or not _allowed(binding) or not valid:
            missing.append(record["path"])
        else:
            record["reviewed_license_policy"] = binding
    return missing


def _paddle(directory):
    modules = {
        name: importlib.import_module(name) for name in ("paddle", "paddlex", "cv2", "paddleocr")
    }
    modules["cv2"].setNumThreads(1)
    if modules["cv2"].getNumThreads() != 1:
        _fail("candidate_resource_invalid")
    arguments = {
        "device": "cpu",
        "cpu_threads": 1,
        "enable_hpi": False,
        "use_doc_orientation_classify": False,
        "use_doc_unwarping": False,
        "use_textline_orientation": False,
        "text_detection_model_name": "PP-OCRv6_small_det",
        "text_detection_model_dir": str(directory / "models/det"),
        "text_recognition_model_name": "PP-OCRv6_small_rec",
        "text_recognition_model_dir": str(directory / "models/rec"),
    }
    pipeline = modules["paddleocr"].PaddleOCR(**arguments)
    try:
        opencv_threads = modules["cv2"].getNumThreads()
        if opencv_threads != 1:
            _fail("candidate_resource_invalid")
        versions = {name: module.__version__ for name, module in modules.items()}
        if versions != {
            "paddle": "3.4.0",
            "paddlex": "3.7.0",
            "cv2": "4.10.0",
            "paddleocr": "3.7.0",
        }:
            _fail("candidate_version_invalid")
        return {
            "versions": versions,
            "model_sets": ["small_det", "small_rec"],
            "arguments": arguments,
            "opencv_threads": opencv_threads,
            "loaded_native": [str(path) for path in _loaded_native()],
        }
    finally:
        pipeline.close()


def _tesseract(directory):
    output = _command(["/opt/tesseract/bin/tesseract", "--version"])
    if not re.search(r"^tesseract 5\.5\.3\b", output) or not re.search(
        r"leptonica-1\.86\.0\b", output
    ):
        _fail("candidate_version_invalid")
    library = ctypes.CDLL("/opt/tesseract/lib/libtesseract.so.5")
    library.TessBaseAPICreate.restype = ctypes.c_void_p
    library.TessBaseAPIInit3.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_char_p]
    library.TessBaseAPIInit3.restype = ctypes.c_int
    for name in ("TessBaseAPIEnd", "TessBaseAPIDelete"):
        getattr(library, name).argtypes = [ctypes.c_void_p]
        getattr(library, name).restype = None
    loaded = set()
    for kind in ("fast", "best"):
        handle = library.TessBaseAPICreate()
        if not handle:
            _fail("candidate_initialization_failed")
        try:
            if library.TessBaseAPIInit3(
                handle, os.fsencode(directory / "tesseract" / kind), b"eng+chi_sim+chi_tra"
            ):
                _fail("candidate_initialization_failed")
            loaded.update(str(path) for path in _loaded_native())
        finally:
            library.TessBaseAPIEnd(handle)
            library.TessBaseAPIDelete(handle)
    return {
        "versions": {"tesseract": "5.5.3", "leptonica": "1.86.0"},
        "version_output": output,
        "model_sets": ["fast", "best"],
        "languages": ["eng", "chi_sim", "chi_tra"],
        "loaded_native": sorted(loaded),
    }


def _save(directory, report):
    path = directory / "report.json"
    temporary = directory / "report.json.tmp"
    try:
        temporary.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf8"
        )
        os.replace(temporary, path)
    except OSError:
        _fail("candidate_output_invalid")


def audit_environment(engine, assets_dir, output_dir):
    """Retain inventory on rejection, and report initialization only after complete validation."""
    if engine not in ("paddle", "tesseract"):
        _fail("candidate_request_invalid")
    directory, output = Path(assets_dir), Path(output_dir)
    try:
        output.mkdir(parents=True, exist_ok=True)
    except OSError:
        _fail("candidate_output_invalid")
    report = {
        "version": 1,
        "engine": engine,
        "status": "failed",
        "reason": None,
        "inference_performed": False,
        "initialization_attempted": False,
        "initialization": None,
    }
    try:
        report["models"] = _models(directory, engine)
        report["source_assets"] = engine_assets._manifest()
        report["runtime"] = _runtime()
        for name in THREADS:
            os.environ[name] = "1"
        os.environ["PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK"] = "True"
        report["thread_environment"] = {name: os.environ[name] for name in THREADS}
        report["pip_check"] = _command([sys.executable, "-m", "pip", "check"])
        provenance = _json(PROVENANCE_PATH)
        report["provenance_identity"] = _identity(PROVENANCE_PATH)
        report["installed"], native_paths, problems = _installed(provenance)
        report["installed_native"] = [
            {"path": str(path), **_identity(path)} for path in sorted(set(native_paths))
        ]
        report["system"] = _system_inventory()
        report["build_notices"] = []
        if engine == "tesseract":
            build = Path("/opt/environment-build/tesseract")
            report["native_build"] = _json(build / "sources.json")
            for name in ("tesseract-LICENSE", "leptonica-LICENSE"):
                path = build / name
                report["build_notices"].append(
                    {"path": str(path), **_identity(path), **_notice_text(path)}
                )
        roots = [Path(sys.executable), *_loaded_native()]
        if engine == "tesseract":
            roots.extend(
                [Path("/opt/tesseract/bin/tesseract"), Path("/opt/tesseract/lib/libtesseract.so.5")]
            )
        report["native"] = _native_inventory(roots)
        _save(output, report)
        if any(record["missing"] for record in report["native"]):
            _fail("candidate_native_unresolved")
        if problems["integrity"] or problems["forbidden"]:
            report["distribution_unresolved"] = problems
            _fail("candidate_license_incomplete")
        if engine == "paddle":
            installed = {
                record["package"].lower(): record["version"] for record in report["installed"]
            }
            if any(installed.get(name) != version for name, version in VERSIONS.items()):
                _fail("candidate_version_invalid")
        report["initialization_attempted"] = True
        with (output / "initialization.log").open("w", encoding="utf8") as log:
            with contextlib.redirect_stdout(log), contextlib.redirect_stderr(log):
                report["initialization"] = (_paddle if engine == "paddle" else _tesseract)(
                    directory
                )
        report["native"] = _native_inventory(
            [*roots, *(Path(path) for path in report["initialization"]["loaded_native"])]
        )
        policy = {"records": []}
        report["native_policy_identities"] = []
        for path in POLICY_PATHS:
            try:
                policy["records"].extend(_json(path)["records"])
                report["native_policy_identities"].append({"name": path.name, **_identity(path)})
            except CandidateEnvironmentError:
                report["native_policy_identities"].append({"name": path.name, "missing": True})
        report["license_unresolved"] = _native_policy(report["native"], policy, _evidence(report))
        report["distribution_unresolved"] = problems["license"]
        if report["license_unresolved"] or problems["license"]:
            _fail("candidate_license_incomplete")
        if any(record["missing"] for record in report["native"]):
            _fail("candidate_native_unresolved")
        report["status"] = "initialized"
        return report
    except CandidateEnvironmentError as error:
        report["reason"] = error.code
        raise
    except Exception:
        report["reason"] = "candidate_initialization_failed"
        _fail(report["reason"])
    finally:
        _save(output, report)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["audit"])
    parser.add_argument("--engine", required=True, choices=["paddle", "tesseract"])
    parser.add_argument("--assets-dir", type=Path, default=Path("/opt/assets"))
    parser.add_argument("--output-dir", type=Path, default=Path("/audit"))
    arguments = parser.parse_args()
    try:
        report = audit_environment(arguments.engine, arguments.assets_dir, arguments.output_dir)
    except CandidateEnvironmentError as error:
        parser.exit(1, error.code + "\n")
    print(
        json.dumps(
            {
                "version": 1,
                "engine": report["engine"],
                "status": report["status"],
                "packages": len(report["installed"]),
                "native": len(report["native"]),
            }
        )
    )


if __name__ == "__main__":
    main()
