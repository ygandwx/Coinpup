"""Fail-closed bundle verification, snapshot lifetime and isolated restoration."""

import hashlib
import io
import json
import os
import sys
from contextlib import contextmanager, nullcontext
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import psycopg
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

import _bundle_archive as bundle  # noqa: E402
import backup_bundle as backup  # noqa: E402
import restore_bundle as restore  # noqa: E402
from _database_archive import ArchiveError, DatabaseTarget  # noqa: E402
from check_bundle_restore import PNG, fictional_pdf, stage_bytes  # noqa: E402

CONTENT = b"Fictional immutable file bytes"
KEY = "f" * 32
DUMP = b"PGDMPfictional database archive"


def metadata(**changes):
    values = {
        "id": str(uuid4()),
        "ledger_id": str(uuid4()),
        "created_by": str(uuid4()),
        "blob_key": KEY,
        "sha256": hashlib.sha256(CONTENT).hexdigest(),
        "byte_size": len(CONTENT),
        "original_filename": "Fictional receipt.pdf",
        "title": "Fictional title",
        "archived": True,
        "version": 2,
        "created_at": "2026-10-03T00:00:00+00:00",
        "updated_at": "2026-10-03T00:01:00+00:00",
        "detected_media_type": "application/pdf",
    }
    return json.dumps(values | changes, separators=(",", ":"))


def manifest(rows):
    return {
        "version": 2,
        "format": "postgresql-custom",
        "scope": "database-and-files",
        "file": "database.dump",
        "sha256": hashlib.sha256(DUMP).hexdigest(),
        "bytes": len(DUMP),
        "stored_files": rows,
        "blobs": bundle.file_descriptors(rows),
    }


@pytest.mark.parametrize(
    "field,value",
    [
        ("blob_key", "../" + "f" * 29),
        ("blob_key", "F" * 32),
        ("blob_key", "f" * 31),
        ("sha256", "0" * 63),
        ("sha256", "Z" * 64),
        ("byte_size", True),
        ("byte_size", 0),
        ("byte_size", -1),
        ("byte_size", 2**63),
        ("id", "not-a-uuid"),
        ("ledger_id", "other/path"),
        ("created_by", None),
    ],
)
def test_path_and_size_metadata_rejected_before_filesystem_access(field, value):
    with pytest.raises(ArchiveError, match="metadata"):
        bundle.file_descriptors([metadata(**{field: value})])


@pytest.mark.parametrize("rows", [None, [None], ["not json"], ["{}"]])
def test_malformed_metadata_is_rejected(rows):
    with pytest.raises(ArchiveError, match="metadata"):
        bundle.file_descriptors(rows)


def test_duplicate_identity_or_blob_key_rejected_but_same_content_in_two_ledgers_allowed():
    first = metadata()
    with pytest.raises(ArchiveError):
        bundle.file_descriptors([first, first])
    with pytest.raises(ArchiveError):
        bundle.file_descriptors([first, metadata()])
    second = metadata(blob_key="a" * 32)
    assert len(bundle.file_descriptors([first, second])) == 2


@pytest.mark.parametrize(
    "changes",
    [
        {"version": 1},
        {"scope": "database-only"},
        {"file": "../database.dump"},
        {"sha256": "bad"},
        {"bytes": True},
        {"bytes": 4},
        {"blobs": {}},
    ],
)
def test_v2_manifest_requires_exact_correspondence_to_stored_files(changes):
    with pytest.raises(ArchiveError):
        bundle.validate_manifest(manifest([metadata()]) | changes)


def test_empty_file_snapshot_is_a_valid_v2_bundle_manifest():
    assert bundle.validate_manifest(manifest([])) == {}


@pytest.mark.parametrize("contents", [CONTENT + b"extra", CONTENT[:-1], b"X" * len(CONTENT)])
def test_copy_rechecks_actual_bytes_instead_of_trusting_prior_verification(tmp_path, contents):
    expected = {"sha256": hashlib.sha256(CONTENT).hexdigest(), "bytes": len(CONTENT)}
    with (tmp_path / "copy").open("wb") as output, pytest.raises(ArchiveError, match="mismatch"):
        bundle.copy_verified(io.BytesIO(contents), output, expected)


def test_copy_preserves_bytes_and_uses_bounded_reads(tmp_path):
    class Stream(io.BytesIO):
        def read(self, size):
            assert 0 < size <= 1024 * 1024
            return super().read(size)

    path = tmp_path / "copy"
    with path.open("wb") as output:
        bundle.copy_verified(
            Stream(CONTENT),
            output,
            {"sha256": hashlib.sha256(CONTENT).hexdigest(), "bytes": len(CONTENT)},
        )
    assert path.read_bytes() == CONTENT


def test_metadata_snapshot_retains_all_fields_and_stable_utc_rendering():
    row, calls = metadata(future_field="Retain this too"), []

    def execute(query):
        calls.append(query)
        return SimpleNamespace(fetchall=lambda: [(row,)])

    assert bundle.stored_file_rows(SimpleNamespace(execute=execute)) == [row]
    assert calls[:2] == ["SET LOCAL TIME ZONE 'UTC'", "SET LOCAL DateStyle = 'ISO, YMD'"]
    assert "row_to_json(f)::text" in calls[-1] and "ORDER BY f.id" in calls[-1]


def test_cli_hides_filesystem_diagnostics(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(backup, "read_target", lambda _: object())
    monkeypatch.setattr(
        backup,
        "backup_bundle",
        lambda *args: (_ for _ in ()).throw(OSError("private financial filename secret")),
    )
    assert backup.main(["--storage-root", str(tmp_path), "--output", str(tmp_path / "out")]) == 1
    captured = capsys.readouterr()
    assert not captured.out and "secret" not in captured.err and "Traceback" not in captured.err


def test_restore_cli_never_calls_existing_database_only_restore(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(restore, "read_target", lambda _: object())
    monkeypatch.setattr(
        restore,
        "restore_bundle",
        lambda *args: (_ for _ in ()).throw(ArchiveError("Bundle verification refused")),
    )
    assert (
        restore.main(
            [
                "--backup",
                str(tmp_path),
                "--storage-root",
                str(tmp_path / "out"),
                "--confirm-empty-database",
                "coinpup_restore_fixture",
            ]
        )
        == 1
    )
    assert "Bundle verification refused" in capsys.readouterr().err


def test_fictional_container_samples_use_actual_storage_signature_checks(tmp_path):
    import asyncio

    from coinpup_api.documents.storage import FileStore

    store = FileStore(tmp_path / "files", 1024 * 1024, 10)
    for contents, media_type in [(fictional_pdf(), "application/pdf"), (PNG, "image/png")]:
        staged = asyncio.run(stage_bytes(store, contents))
        assert staged.media_type == media_type
        key = store.publish(staged)
        with store.open_blob(key, staged.sha256, staged.byte_size) as original:
            assert original.read() == contents


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    if os.name != "posix":
        pytest.skip("Descriptor-relative bundle IO and permissions require Linux CI")
    storage = tmp_path / "storage"
    storage.mkdir()
    (storage / "blobs").mkdir()
    (storage / "blobs" / KEY).write_bytes(CONTENT)
    rows = [metadata()]
    events, state = [], {"active": False, "rows": rows, "version": 170006}

    class Connection:
        def execute(self, query):
            events.append(query)
            if query == "SHOW server_version_num":
                return SimpleNamespace(fetchone=lambda: (str(state["version"]),))
            if query == "SELECT pg_export_snapshot()":
                return SimpleNamespace(fetchone=lambda: ("00000001-00000002-1",))
            if "row_to_json" in query:
                return SimpleNamespace(fetchall=lambda: [(row,) for row in state["rows"]])

    @contextmanager
    def connect(*args, **kwargs):
        state["active"] = True
        try:
            yield Connection()
        finally:
            state["active"] = False

    target = DatabaseTarget(
        "localhost", 5432, "fixture", "coinpup_restore_bundle_fixture", "secret"
    )
    monkeypatch.setattr(DatabaseTarget, "connect", connect)
    monkeypatch.setattr(backup, "tool_environment", lambda _: nullcontext({}))
    monkeypatch.setattr(restore, "tool_environment", lambda _: nullcontext({}))
    monkeypatch.setattr(restore, "empty_target_guard", lambda _: nullcontext())

    def tool(arguments, **kwargs):
        events.append(arguments)
        if arguments[0] == "pg_dump":
            assert state["active"], "Exporting transaction must survive the entire pg_dump"
            assert "--snapshot=00000001-00000002-1" in arguments
            kwargs["stdout"].write(DUMP)

    monkeypatch.setattr(backup, "run_tool", tool)
    monkeypatch.setattr(restore, "run_tool", tool)
    return SimpleNamespace(
        storage=storage, target=target, rows=rows, state=state, events=events, root=tmp_path
    )


def make_bundle(runtime):
    output = runtime.root / "bundle"
    backup.backup_bundle(runtime.target, runtime.storage, output)
    return output


def test_exported_snapshot_stays_alive_and_only_referenced_blobs_are_copied(runtime):
    (runtime.storage / "blobs" / ("a" * 32)).write_bytes(b"orphan")
    (runtime.storage / "staging").mkdir()
    (runtime.storage / "staging" / "fragment").write_bytes(b"not complete")
    output = make_bundle(runtime)
    assert runtime.events[0] == "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"
    assert not runtime.state["active"]
    assert set(path.name for path in (output / "blobs").iterdir()) == {KEY}
    with bundle.verified_bundle(output) as (stored, dump, _blobs):
        assert stored["stored_files"] == runtime.rows
        assert dump.read() == DUMP
    assert output.stat().st_mode & 0o777 == 0o700
    for path in [output / "manifest.json", output / "database.dump", output / "blobs" / KEY]:
        assert path.stat().st_mode & 0o777 == 0o600


def test_zero_files_does_not_require_or_create_lazy_source_directories(runtime):
    runtime.state["rows"] = []
    empty = runtime.root / "empty-storage"
    empty.mkdir()
    output = runtime.root / "empty-bundle"
    backup.backup_bundle(runtime.target, empty, output)
    assert list(empty.iterdir()) == []
    with bundle.verified_bundle(output) as (stored, _dump, _blobs):
        assert stored["stored_files"] == [] and stored["blobs"] == {}


@pytest.mark.parametrize("problem", ["missing", "corrupt", "symlink", "fifo", "missing_directory"])
def test_invalid_referenced_blob_never_gets_completion_manifest(runtime, problem):
    source = runtime.storage / "blobs" / KEY
    source.unlink()
    if problem == "corrupt":
        source.write_bytes(b"X" * len(CONTENT))
    elif problem == "symlink":
        other = runtime.root / "outside"
        other.write_bytes(CONTENT)
        source.symlink_to(other)
    elif problem == "fifo":
        os.mkfifo(source)
    elif problem == "missing_directory":
        source.parent.rmdir()
    with pytest.raises((ArchiveError, OSError)):
        make_bundle(runtime)
    assert not (runtime.root / "bundle" / "manifest.json").exists()


def test_symlink_in_source_parent_is_not_followed(runtime):
    shortcut = runtime.root / "shortcut"
    shortcut.symlink_to(runtime.storage, target_is_directory=True)
    with pytest.raises(OSError):
        backup.backup_bundle(runtime.target, shortcut, runtime.root / "out")
    assert not (runtime.root / "out").exists()


def test_pg_failure_does_not_mark_backup_complete(runtime, monkeypatch):
    monkeypatch.setattr(
        backup, "run_tool", lambda *a, **k: (_ for _ in ()).throw(ArchiveError("pg_dump failed"))
    )
    with pytest.raises(ArchiveError, match="pg_dump failed"):
        make_bundle(runtime)
    assert not (runtime.root / "bundle" / "manifest.json").exists()


def test_manifest_write_failure_never_publishes_completion_marker(runtime, monkeypatch):
    actual = backup.sync_output

    def fail_manifest(output):
        if output.tell() > len(DUMP):
            raise OSError("disk failure")
        actual(output)

    monkeypatch.setattr(backup, "sync_output", fail_manifest)
    with pytest.raises(OSError):
        make_bundle(runtime)
    assert not (runtime.root / "bundle" / "manifest.json").exists()


def test_database_errors_are_redacted(runtime, monkeypatch):
    monkeypatch.setattr(
        backup,
        "stored_file_rows",
        lambda _: (_ for _ in ()).throw(psycopg.OperationalError("private credentials")),
    )
    with pytest.raises(ArchiveError) as error:
        make_bundle(runtime)
    assert "private" not in str(error.value)


@pytest.mark.parametrize("problem", ["missing", "corrupt", "symlink", "extra", "dump"])
def test_incomplete_bundle_refused_before_target_creation_or_restore(runtime, monkeypatch, problem):
    output = make_bundle(runtime)
    path = output / "blobs" / KEY
    if problem == "missing":
        path.unlink()
    elif problem == "corrupt":
        path.write_bytes(b"X" * len(CONTENT))
    elif problem == "symlink":
        path.unlink()
        path.symlink_to(runtime.storage / "blobs" / KEY)
    elif problem == "extra":
        (output / "blobs" / ("b" * 32)).write_bytes(b"unmanifested")
    else:
        (output / "database.dump").write_bytes(b"PGDMPcorrupt")
    monkeypatch.setattr(restore, "run_tool", lambda *a, **k: pytest.fail("Restore must not run"))
    destination = runtime.root / "restored"
    with pytest.raises(ArchiveError):
        restore.restore_bundle(runtime.target, output, destination, runtime.target.database)
    assert not destination.exists()


def test_restore_copies_and_syncs_blobs_before_single_transaction_database_restore(
    runtime, monkeypatch
):
    output = make_bundle(runtime)
    destination, calls = runtime.root / "restored", []

    def tool(arguments, **kwargs):
        calls.append(arguments)
        if "--list" not in arguments:
            assert (destination / "blobs" / KEY).read_bytes() == CONTENT
            assert (destination / "staging").is_dir()
            assert "--single-transaction" in arguments and "--exit-on-error" in arguments
            assert not {"--clean", "--create"}.intersection(arguments)
            assert kwargs["stdin"].read() == DUMP

    monkeypatch.setattr(restore, "run_tool", tool)
    restore.restore_bundle(runtime.target, output, destination, runtime.target.database)
    assert len(calls) == 2
    assert set(path.name for path in destination.iterdir()) == {"blobs", "staging"}


def test_restore_keeps_verified_dump_descriptor_if_its_path_is_replaced(runtime, monkeypatch):
    output = make_bundle(runtime)

    def tool(arguments, **kwargs):
        if "--list" in arguments:
            (output / "database.dump").unlink()
            (output / "database.dump").write_bytes(b"PGDMPsubstituted")
        else:
            assert kwargs["stdin"].read() == DUMP

    monkeypatch.setattr(restore, "run_tool", tool)
    restore.restore_bundle(
        runtime.target, output, runtime.root / "restored", runtime.target.database
    )


def test_blob_replacement_after_preflight_is_rechecked_before_database_restore(
    runtime, monkeypatch
):
    output = make_bundle(runtime)

    def tool(arguments, **kwargs):
        assert "--list" in arguments, "Database restore must not run with substituted bytes"
        (output / "blobs" / KEY).write_bytes(b"X" * len(CONTENT))

    monkeypatch.setattr(restore, "run_tool", tool)
    with pytest.raises(ArchiveError, match="mismatch"):
        restore.restore_bundle(
            runtime.target, output, runtime.root / "restored", runtime.target.database
        )


def test_existing_output_and_existing_restore_root_are_never_overwritten(runtime, monkeypatch):
    output = make_bundle(runtime)
    original = (output / "manifest.json").read_bytes()
    with pytest.raises(FileExistsError):
        backup.backup_bundle(runtime.target, runtime.storage, output)
    assert (output / "manifest.json").read_bytes() == original
    target = runtime.root / "existing-empty"
    target.mkdir()
    monkeypatch.setattr(restore, "run_tool", lambda *a, **k: pytest.fail("must not run"))
    with pytest.raises(ArchiveError, match="already exists"):
        restore.restore_bundle(runtime.target, output, target, runtime.target.database)
    assert list(target.iterdir()) == []


def test_nonempty_database_refusal_does_not_create_destination(runtime, monkeypatch):
    output = make_bundle(runtime)

    @contextmanager
    def refusal(_):
        raise ArchiveError("Target database is not empty")
        yield

    monkeypatch.setattr(restore, "empty_target_guard", refusal)
    destination = runtime.root / "restored"
    with pytest.raises(ArchiveError, match="not empty"):
        restore.restore_bundle(runtime.target, output, destination, runtime.target.database)
    assert not destination.exists()


def test_restored_metadata_mismatch_is_failure_and_never_cleans_target(runtime, monkeypatch):
    output = make_bundle(runtime)
    runtime.state["rows"] = []
    destination = runtime.root / "restored"
    with pytest.raises(ArchiveError, match="metadata does not match"):
        restore.restore_bundle(runtime.target, output, destination, runtime.target.database)
    assert (destination / "blobs" / KEY).read_bytes() == CONTENT


def test_pg_restore_failure_retains_isolated_files_for_inspection(runtime, monkeypatch):
    output = make_bundle(runtime)

    def tool(arguments, **kwargs):
        if "--list" not in arguments:
            raise ArchiveError("pg_restore failed")

    monkeypatch.setattr(restore, "run_tool", tool)
    destination = runtime.root / "restored"
    with pytest.raises(ArchiveError, match="pg_restore failed"):
        restore.restore_bundle(runtime.target, output, destination, runtime.target.database)
    assert (destination / "blobs" / KEY).read_bytes() == CONTENT
