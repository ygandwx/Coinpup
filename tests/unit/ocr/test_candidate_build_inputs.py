"""Small fictional source archives exercise frozen notice extraction, never real Python builds."""

import hashlib
import importlib.util
import io
import json
import tarfile
from pathlib import Path

import pytest

SOURCE = Path(__file__).resolve().parents[3] / "ops/ocr-benchmark/build_inputs.py"
SPEC = importlib.util.spec_from_file_location("coinpup_candidate_build_inputs", SOURCE)
assert SPEC and SPEC.loader
build_inputs = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(build_inputs)

PREFIX = "Python-3.12.14/"
DATA = [
    (PREFIX + "LICENSE", b"Fictional source notice.\n", "utf-8"),
    (PREFIX + "Modules/vendor/LICENSE", b"Fictional original \xe9 notice.\n", "latin-1"),
]


@pytest.fixture
def archive(tmp_path, monkeypatch):
    def make(entries=None, members=None):
        directory = tmp_path / "sources"
        directory.mkdir(exist_ok=True)
        path = directory / "fictional-python.tar.xz"
        with tarfile.open(path, "w:xz") as stream:
            for name, data, kind in entries or [
                (name, data, tarfile.REGTYPE) for name, data, _ in DATA
            ]:
                item = tarfile.TarInfo(name)
                item.type = kind
                if kind == tarfile.SYMTYPE:
                    item.linkname = "../fictional-outside"
                if kind == tarfile.REGTYPE:
                    item.size = len(data)
                stream.addfile(item, io.BytesIO(data) if item.isfile() else None)
        frozen = (
            members
            if members is not None
            else [
                {
                    "member": name,
                    "output_name": f"fictional-notice-{index}",
                    "byte_size": len(data),
                    "sha256": hashlib.sha256(data).hexdigest(),
                    "encoding": encoding,
                }
                for index, (name, data, encoding) in enumerate(DATA)
            ]
        )
        definition = {
            "name": path.name,
            "version": "3.12.14",
            "members": frozen,
            "byte_size": path.stat().st_size,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        }
        monkeypatch.setattr(build_inputs, "manifest", lambda: {"python_source": definition})
        return directory, definition

    return make


def test_frozen_notices_preserve_complete_original_bytes_and_source_manifest(archive, tmp_path):
    entries = [(name, data, tarfile.REGTYPE) for name, data, _ in DATA]
    entries.append((PREFIX + "ignored-example.txt", b"Not a selected notice", tarfile.REGTYPE))
    source, frozen = archive(entries)
    output = tmp_path / "notices"
    result = build_inputs.python_notices(source, output)
    assert result["python_source"] == frozen
    assert json.loads((output / "manifest.json").read_text(encoding="utf8")) == result
    assert {path.name for path in output.iterdir()} == {
        "manifest.json",
        "fictional-notice-0",
        "fictional-notice-1",
    }
    for notice, (_, original, _) in zip(result["notices"], DATA, strict=True):
        assert Path(notice["path"]).read_bytes() == original
        assert original.decode(notice["encoding"]).encode(notice["encoding"]) == original
        assert notice["sha256"] == hashlib.sha256(original).hexdigest()
        assert notice["byte_size"] == len(original)


def test_missing_frozen_member_cannot_publish_a_verified_manifest(archive, tmp_path):
    name, data, _ = DATA[0]
    source, _ = archive([(name, data, tarfile.REGTYPE)])
    with pytest.raises(ValueError, match="^python_notice_missing$"):
        build_inputs.python_notices(source, tmp_path / "notices")
    assert not (tmp_path / "notices/manifest.json").exists()


def test_damaged_member_is_rejected_even_when_archive_identity_matches(archive, tmp_path):
    entries = [(name, data, tarfile.REGTYPE) for name, data, _ in DATA]
    name, original, _ = entries[0]
    entries[0] = (name, b"X" * len(original), tarfile.REGTYPE)
    source, _ = archive(entries)
    with pytest.raises(ValueError, match="^python_notice_invalid$"):
        build_inputs.python_notices(source, tmp_path / "notices")
    assert not (tmp_path / "notices/manifest.json").exists()


def test_duplicate_tar_member_cannot_override_the_first_original_notice(archive, tmp_path):
    entries = [(name, data, tarfile.REGTYPE) for name, data, _ in DATA]
    entries.append(entries[0])
    source, _ = archive(entries)
    with pytest.raises(ValueError, match="^python_notice_invalid$"):
        build_inputs.python_notices(source, tmp_path / "notices")
    assert (tmp_path / "notices/fictional-notice-0").read_bytes() == DATA[0][1]
    assert not (tmp_path / "notices/manifest.json").exists()


@pytest.mark.parametrize(
    "name,kind",
    [
        (PREFIX + "../fictional-outside", tarfile.REGTYPE),
        (PREFIX + "unused-symlink", tarfile.SYMTYPE),
        (PREFIX + "unused-fifo", tarfile.FIFOTYPE),
    ],
)
def test_unselected_traversal_symlink_and_nonregular_members_still_rejected(
    archive, tmp_path, name, kind
):
    entries = [
        (name, b"malicious fictional payload", kind),
        *[(member, data, tarfile.REGTYPE) for member, data, _ in DATA],
    ]
    source, _ = archive(entries)
    with pytest.raises(ValueError, match="^python_notice_invalid$"):
        build_inputs.python_notices(source, tmp_path / "notices")
    assert not (tmp_path / "fictional-outside").exists()
    assert not (tmp_path / "notices/manifest.json").exists()


def test_per_member_and_total_frozen_byte_budgets_reject_before_copying(archive, tmp_path):
    for label, count, size, code in [
        ("member", 1, 1024**2 + 1, "python_notice_invalid"),
        ("total", 5, 1024**2, "python_notice_limit"),
    ]:
        members = [
            {
                "member": PREFIX + f"notice-{index}",
                "output_name": f"notice-{index}",
                "byte_size": size,
                "sha256": "a" * 64,
                "encoding": "utf-8",
            }
            for index in range(count)
        ]
        source, _ = archive(members=members)
        output = tmp_path / label
        with pytest.raises(ValueError, match="^" + code + "$"):
            build_inputs.python_notices(source, output)
        assert not output.exists()


def test_distinct_members_cannot_collide_at_the_same_output_name(archive, tmp_path):
    source, frozen = archive()
    frozen["members"][1]["output_name"] = frozen["members"][0]["output_name"]
    with pytest.raises(ValueError, match="^python_notice_invalid$"):
        build_inputs.python_notices(source, tmp_path / "notices")
    assert not (tmp_path / "notices").exists()


def test_symlink_output_is_refused_before_any_notice_write(archive, tmp_path, monkeypatch):
    source, _ = archive()
    output = tmp_path / "linked-output"
    original = Path.is_symlink
    monkeypatch.setattr(Path, "is_symlink", lambda path: path == output or original(path))
    with pytest.raises(ValueError, match="^build_input_invalid$"):
        build_inputs.python_notices(source, output)
    assert not output.exists()
