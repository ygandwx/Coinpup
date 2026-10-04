"""Inspect real pinned inputs offline; this is neither inference nor a closure audit."""

import email.policy
import hashlib
import json
import os
import struct
import sys
import zipfile
from email.parser import BytesParser
from pathlib import Path, PurePosixPath

import pytest

from scripts.ocr_benchmark import engine_assets as assets

pytestmark = [pytest.mark.ocr, pytest.mark.engine_assets]

PACKAGES = {
    "opencv-contrib-python": ("4.10.0.84", "cp37-abi3-manylinux_2_17_x86_64"),
    "paddleocr": ("3.7.0", "py3-none-any"),
    "paddlepaddle": ("3.4.0", "cp312-cp312-linux_x86_64"),
    "paddlex": ("3.7.0", "py3-none-any"),
}


def identity(stream):
    digest, size = hashlib.sha256(), 0
    while chunk := stream.read(65536):
        digest.update(chunk)
        size += len(chunk)
    return size, digest.hexdigest()


@pytest.fixture(scope="module")
def pinned():
    configured = os.environ.get("COINPUP_ENGINE_ASSETS")
    assert configured, "COINPUP_ENGINE_ASSETS must identify the explicitly acquired pinned inputs."
    root = Path(configured).resolve()
    assert root.is_dir(), "The configured pinned input directory must exist."
    manifest = assets.verify_assets(root)
    inspection = assets.inspect_wheels(root)
    return root, manifest, inspection


def test_every_pinned_file_has_its_independent_size_and_sha256(pinned, capsys):
    root, manifest, inspection = pinned
    assert manifest["version"] == 1
    assert manifest["target"] == "CPython 3.12 Linux x86_64 CPU"
    assert "not an installed or loaded native dependency chain" in manifest["scope"]
    assert len(manifest["files"]) == 53
    assert sum(item["byte_size"] for item in manifest["files"]) == 349153939
    names = [item["name"] for item in manifest["files"]]
    assert len(names) == len(set(names))
    for item in manifest["files"]:
        relative = PurePosixPath(item["name"])
        assert not relative.is_absolute() and ".." not in relative.parts
        path = root.joinpath(*relative.parts)
        assert path.is_file() and not path.is_symlink() and path.resolve().is_relative_to(root)
        with path.open("rb") as stream:
            assert identity(stream) == (item["byte_size"], item["sha256"])
    with capsys.disabled():
        print(
            "ENGINE_ASSETS "
            + json.dumps(
                {
                    "files": 53,
                    "bytes": 349153939,
                    "wheels": 4,
                    "paddle_native": 18,
                    "scope": "pinned_inputs_only",
                },
                sort_keys=True,
            )
        )
    assert len(inspection["wheels"]) == 4


@pytest.mark.parametrize("role", ["det", "rec"])
def test_real_model_files_and_their_actual_license_declaration(pinned, role):
    root, manifest, _ = pinned
    records = [item for item in manifest["files"] if item["name"].startswith(f"models/{role}/")]
    assert {Path(item["name"]).name for item in records} == {
        ".gitattributes",
        "README.md",
        "inference.json",
        "inference.yml",
        "inference.pdiparams",
    }
    assert all(item["role"] == "model" for item in records)
    directory = root / "models" / role
    assert isinstance(json.loads((directory / "inference.json").read_text(encoding="utf8")), dict)
    assert f"model_name: PP-OCRv6_small_{role}" in (directory / "inference.yml").read_text(
        encoding="utf8"
    )
    weights = directory / "inference.pdiparams"
    assert weights.stat().st_size > 1024**2
    with weights.open("rb") as stream:
        assert not stream.read(128).startswith(b"version https://git-lfs.github.com/spec/")
    declaration = manifest["model_licenses"][role]
    assert declaration == {
        "declaration_file": f"models/{role}/README.md",
        "declared_license": "apache-2.0",
        "standalone_LICENSE_present": False,
    }
    assert "license: apache-2.0" in (directory / "README.md").read_text(encoding="utf8")
    assert not (directory / "LICENSE").exists()


def test_tesseract_source_is_exactly_553_and_all_three_language_sets_are_real(pinned):
    root, manifest, _ = pinned
    source = next(item for item in manifest["files"] if item["role"] == "source_archive")
    commit = "db0ec62f81b0737fbbe184d8fea40af5738f8eef"
    record = next(item for item in manifest["source_commits"] if item["repository"] == "tesseract")
    assert record["tag"] == "5.5.3" and record["commit"] == commit
    with zipfile.ZipFile(root / source["name"]) as archive:
        prefix = f"tesseract-{commit}/"
        assert archive.read(prefix + "VERSION") == b"5.5.3\n"
        assert (
            archive.read(prefix + "CMakeLists.txt")
            == (root / "tesseract/source/CMakeLists.txt").read_bytes()
        )
        assert b"Apache License" in archive.read(prefix + "LICENSE")
    trained = [item for item in manifest["files"] if item["role"] == "traineddata"]
    assert {item["name"] for item in trained} == {
        f"tesseract/{kind}/{lang}.traineddata"
        for kind in ("fast", "best")
        for lang in ("eng", "chi_sim", "chi_tra")
    }
    for item in trained:
        with (root / item["name"]).open("rb") as stream:
            count = struct.unpack("<I", stream.read(4))[0]
            assert count == 24
            offsets = struct.unpack("<" + "q" * count, stream.read(8 * count))
        assert any(offset >= 4 + count * 8 for offset in offsets)
        assert all(
            offset == -1 or 4 + count * 8 <= offset < item["byte_size"] for offset in offsets
        )


def test_four_wheels_preserve_real_metadata_dependency_markers_and_complete_notices(pinned):
    root, _, inspection = pinned
    assert {item["package"] for item in inspection["wheels"]} == set(PACKAGES)
    parser = BytesParser(policy=email.policy.compat32)
    for wheel in inspection["wheels"]:
        version, tag = PACKAGES[wheel["package"]]
        assert wheel["version"] == version and tag in wheel["tags"]
        with zipfile.ZipFile(root / wheel["name"]) as archive:
            metadata = parser.parsebytes(archive.read(wheel["metadata"]["name"]))
            assert wheel["requires_dist"] == metadata.get_all("Requires-Dist", [])
            assert wheel["license"] == metadata.get("License")
            assert wheel["license_expression"] == metadata.get("License-Expression")
            assert wheel["license_files"] == metadata.get_all("License-File", [])
            base = wheel["metadata"]["name"].rsplit("/", 1)[0]
            notices = {record["name"]: record for record in wheel["notices"]}
            for name in wheel["license_files"]:
                assert any(
                    candidate in notices
                    for candidate in (base + "/" + name, base + "/licenses/" + name)
                )
            for record in [wheel["metadata"], wheel["wheel"], *wheel["notices"]]:
                actual = archive.read(record["name"])
                assert actual == record["text"].encode("utf8")
                assert (len(actual), hashlib.sha256(actual).hexdigest()) == (
                    record["byte_size"],
                    record["sha256"],
                )
            assert wheel["license_files"] and notices
    paddle = next(wheel for wheel in inspection["wheels"] if wheel["package"] == "paddlepaddle")
    assert any(record["name"].endswith("/AUTHORS.md") for record in paddle["notices"])


def test_paddle_native_inventory_is_complete_and_independently_stream_hashed(pinned):
    root, manifest, inspection = pinned
    paddle = next(wheel for wheel in inspection["wheels"] if wheel["package"] == "paddlepaddle")
    expected = manifest["native_expected"][paddle["name"]]
    assert len(expected) == len(paddle["native"]) == 18
    assert paddle["native"] == sorted(expected, key=lambda record: record["name"])
    assert {Path(record["name"]).name for record in expected} >= {
        "libmklml_intel.so",
        "libiomp5.so",
        "libgfortran.so.3",
        "libquadmath.so.0",
        "libopenvino.so.2500",
        "libpaddle.so",
        "libdnnl.so.3",
        "libtbb.so.12",
    }
    with zipfile.ZipFile(root / paddle["name"]) as archive:
        for record in expected:
            with archive.open(record["name"]) as stream:
                assert identity(stream) == (record["byte_size"], record["sha256"])


def test_full_upstream_notices_and_embedded_mkl_sources_remain_honestly_scoped(pinned):
    root, manifest, _ = pinned
    runtime = (root / "native-notices/gcc/COPYING.RUNTIME").read_text(encoding="utf8")
    assert "GCC RUNTIME LIBRARY EXCEPTION" in runtime
    assert "GNU GENERAL PUBLIC LICENSE" in (root / "native-notices/gcc/COPYING3").read_text(
        encoding="utf8"
    )
    assert "GNU LESSER GENERAL PUBLIC LICENSE" in (
        root / "native-notices/gcc/libquadmath/COPYING.LIB"
    ).read_text(encoding="utf8")
    embedded = manifest["embedded_notices"]
    assert {record["name"] for record in embedded} == {
        "mklml/license.txt",
        "mklml/third-party-programs.txt",
    }
    for record in embedded:
        data = record["text"].encode(record["text_encoding"])
        assert (len(data), hashlib.sha256(data).hexdigest()) == (
            record["byte_size"],
            record["sha256"],
        )
        assert record["archive_member"].endswith(record["name"].split("/")[-1])
        source = record["source_archive"]
        assert source["url"].startswith("https://") and source["byte_size"] > 1024**2
        assert len(source["sha256"]) == 64
    assert "Intel Simplified Software License" in embedded[0]["text"]
    assert (
        "not a byte-identical binary provenance claim"
        in (manifest["inspection_context"]["paddle_mkl"]["correspondence"])
    )


def test_offline_inspection_never_imports_engine_or_native_runtime(pinned):
    assert pinned[2]["version"] == 1
    forbidden = ("paddle", "paddleocr", "paddlex", "cv2")
    assert not any(
        name == prefix or name.startswith(prefix + ".")
        for name in sys.modules
        for prefix in forbidden
    )
