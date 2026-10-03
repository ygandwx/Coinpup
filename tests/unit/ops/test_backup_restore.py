"""Exercise refusal and failure behavior without touching a PostgreSQL server."""

import hashlib
import json
import os
import subprocess
import sys
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace

import psycopg
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "scripts"))

import _database_archive as archive  # noqa: E402
import backup_database as backup  # noqa: E402
import restore_database as restore  # noqa: E402

TARGET = archive.DatabaseTarget("localhost", 5432, "fixture", "coinpup_restore_test", "secret:pw")


@pytest.fixture
def archive_directory(tmp_path):
    directory = tmp_path / "archive"
    directory.mkdir()
    contents = b"PGDMP\x00test fixture archive"
    (directory / "database.dump").write_bytes(contents)
    (directory / "manifest.json").write_text(
        json.dumps(
            {
                "version": 1,
                "format": "postgresql-custom",
                "scope": "database-only",
                "file": "database.dump",
                "sha256": hashlib.sha256(contents).hexdigest(),
                "bytes": len(contents),
            }
        )
    )
    return directory


@pytest.fixture
def simulated_runtime(monkeypatch):
    monkeypatch.setattr(archive, "require_posix", lambda: None)
    monkeypatch.setattr(restore, "require_posix", lambda: None)
    monkeypatch.setattr(backup, "require_posix", lambda: None)
    monkeypatch.setattr(restore, "tool_environment", lambda target: nullcontext({}))


class QueryConnection:
    def __init__(self, rows):
        self.rows = iter(rows)

    def execute(self, *args):
        row = next(self.rows)
        if isinstance(row, Exception):
            raise row
        return SimpleNamespace(fetchone=lambda: (row,))


def connect_results(monkeypatch, rows):
    monkeypatch.setattr(
        archive.DatabaseTarget, "connect", lambda *a, **k: nullcontext(QueryConnection(rows))
    )


@pytest.mark.parametrize("contents", [b"PGDMPtampered", b"not a dump"])
def test_corrupt_backup_never_connects_or_runs_restore(
    monkeypatch, archive_directory, simulated_runtime, contents
):
    (archive_directory / "database.dump").write_bytes(contents)
    monkeypatch.setattr(restore, "run_tool", lambda *a, **k: pytest.fail("tool must not run"))
    with pytest.raises(archive.ArchiveError, match="SHA-256"):
        restore.restore_database(TARGET, archive_directory, TARGET.database)


@pytest.mark.parametrize(
    "rows,message",
    [
        ([True, TARGET.database, True], "not empty"),
        ([True, TARGET.database, False, True], "other connections"),
        ([False], "Another Coinpup restore"),
        ([True, "production"], "does not match"),
    ],
)
def test_target_refusal_does_not_restore_or_remove_data(
    monkeypatch, archive_directory, simulated_runtime, rows, message
):
    calls = []
    monkeypatch.setattr(restore, "run_tool", lambda args, **kwargs: calls.append(args))
    connect_results(monkeypatch, rows)
    with pytest.raises(archive.ArchiveError, match=message):
        restore.restore_database(TARGET, archive_directory, TARGET.database)
    assert calls == [["pg_restore", "--list"]]


@pytest.mark.parametrize(
    "database,confirmation",
    [
        ("production", "production"),
        ("postgres", "postgres"),
        ("coinpup_restore_test", "coinpup_restore_other"),
    ],
)
def test_explicit_disposable_target_required_before_any_io(database, confirmation):
    target = archive.DatabaseTarget("localhost", 5432, "fixture", database, "secret")
    with pytest.raises(archive.ArchiveError, match="new database"):
        archive.validate_disposable_target(target, confirmation)


def test_failed_preflight_database_query_hides_server_diagnostics(monkeypatch):
    connect_results(monkeypatch, [psycopg.OperationalError("password=secret connection failed")])
    with pytest.raises(archive.ArchiveError) as caught, archive.empty_target_guard(TARGET):
        pytest.fail("guard must not allow restore")
    assert "secret" not in str(caught.value)


def test_archive_tool_failure_never_becomes_success(monkeypatch, capsys):
    def failed_tool(*args, **kwargs):
        assert kwargs["stderr"] == subprocess.DEVNULL
        return SimpleNamespace(returncode=7)

    monkeypatch.setattr(archive.subprocess, "run", failed_tool)
    with pytest.raises(archive.ArchiveError, match="exit 7"):
        archive.run_tool(["pg_restore"], environment={})
    assert "secret" not in capsys.readouterr().err


def test_failed_restore_returns_failure_without_success_message(
    monkeypatch, archive_directory, simulated_runtime, capsys
):
    def failed_restore(args, **kwargs):
        if "--list" not in args:
            raise archive.ArchiveError("pg_restore failed (exit 9)")

    monkeypatch.setattr(restore, "run_tool", failed_restore)
    monkeypatch.setattr(restore, "read_target", lambda variable: TARGET)
    connect_results(monkeypatch, [True, TARGET.database, False, False])
    assert (
        restore.main(
            ["--backup", str(archive_directory), "--confirm-empty-database", TARGET.database]
        )
        == 1
    )
    output = capsys.readouterr()
    assert not output.out
    assert "exit 9" in output.err


def test_restore_uses_transaction_and_never_clean_or_create(
    monkeypatch, archive_directory, simulated_runtime
):
    calls = []
    monkeypatch.setattr(restore, "run_tool", lambda args, **kwargs: calls.append(args))
    connect_results(monkeypatch, [True, TARGET.database, False, False])
    restore.restore_database(TARGET, archive_directory, TARGET.database)
    assert "--single-transaction" in calls[-1]
    assert "--exit-on-error" in calls[-1]
    assert not {"--clean", "--create"}.intersection(calls[-1])
    assert TARGET.password not in str(calls)


def test_failed_dump_leaves_no_completed_manifest(monkeypatch, tmp_path, simulated_runtime):
    monkeypatch.setattr(backup, "tool_environment", lambda target: nullcontext({}))

    def fail(*args, **kwargs):
        kwargs["stdout"].write(b"PGDMPincomplete")
        raise archive.ArchiveError("pg_dump failed")

    monkeypatch.setattr(backup, "run_tool", fail)
    output = tmp_path / "failed"
    with pytest.raises(archive.ArchiveError, match="pg_dump failed"):
        backup.backup_database(TARGET, output)
    assert not (output / "manifest.json").exists()
    with pytest.raises(archive.ArchiveError), archive.verified_archive(output):
        pytest.fail("incomplete dump must not be usable")


def test_existing_output_is_never_overwritten(monkeypatch, archive_directory, simulated_runtime):
    before = (archive_directory / "database.dump").read_bytes()
    monkeypatch.setattr(backup, "run_tool", lambda *a, **k: pytest.fail("must not run"))
    with pytest.raises(FileExistsError):
        backup.backup_database(TARGET, archive_directory)
    assert (archive_directory / "database.dump").read_bytes() == before


def test_credentials_do_not_enter_argv_or_child_environment(
    monkeypatch, tmp_path, simulated_runtime
):
    monkeypatch.setenv("PGPASSWORD", "old password")
    monkeypatch.setenv("PGSERVICE", "unexpected-default")
    monkeypatch.setenv("COINPUP_DATABASE_URL", "postgresql://secret@production")
    with archive.tool_environment(TARGET) as environment:
        assert "PGPASSWORD" not in environment
        assert "PGSERVICE" not in environment
        assert "COINPUP_DATABASE_URL" not in environment
        assert TARGET.password not in str(environment)
        passfile = Path(environment["PGPASSFILE"])
        assert passfile.read_text() == "*:*:*:*:secret\\:pw\n"
        if os.name == "posix":
            assert passfile.stat().st_mode & 0o777 == 0o600
            assert passfile.parent.stat().st_mode & 0o777 == 0o700
    assert not passfile.exists()


def test_invalid_connection_url_does_not_echo_secret(monkeypatch):
    monkeypatch.setenv(
        "ARCHIVE_TEST_URL", "postgresql://fixture:TOP_SECRET@localhost/db?options=bad"
    )
    with pytest.raises(archive.ArchiveError) as caught:
        archive.read_target("ARCHIVE_TEST_URL")
    assert "TOP_SECRET" not in str(caught.value)


def test_preflight_cannot_be_redirected_by_inherited_libpq_defaults(monkeypatch):
    monkeypatch.setenv("PGHOSTADDR", "192.0.2.99")
    monkeypatch.setenv("PGSERVICE", "unintended-server")

    def connect(**kwargs):
        assert "PGHOSTADDR" not in os.environ
        assert "PGSERVICE" not in os.environ
        assert kwargs["host"] == TARGET.host
        raise psycopg.OperationalError("connection failed")

    monkeypatch.setattr(archive.psycopg, "connect", connect)
    with pytest.raises(psycopg.OperationalError):
        TARGET.connect()
    assert os.environ["PGHOSTADDR"] == "192.0.2.99"
    assert os.environ["PGSERVICE"] == "unintended-server"


def test_cli_failure_status_and_no_traceback(monkeypatch, tmp_path, capsys, simulated_runtime):
    monkeypatch.setattr(restore, "read_target", lambda variable: TARGET)
    code = restore.main(
        ["--backup", str(tmp_path / "missing"), "--confirm-empty-database", TARGET.database]
    )
    assert code == 1
    output = capsys.readouterr()
    assert "restored" not in output.out
    assert "Traceback" not in output.err


@pytest.mark.skipif(os.name != "posix", reason="Real POSIX permissions require Linux CI")
def test_backup_permissions_and_manifest(monkeypatch, tmp_path):
    def write_dump(*args, **kwargs):
        kwargs["stdout"].write(b"PGDMPfixture")

    monkeypatch.setattr(backup, "run_tool", write_dump)
    directory = tmp_path / "backup"
    backup.backup_database(TARGET, directory)
    assert directory.stat().st_mode & 0o777 == 0o700
    for filename in ("database.dump", "manifest.json"):
        assert (directory / filename).stat().st_mode & 0o777 == 0o600
    with archive.verified_archive(directory) as source:
        assert source.read() == b"PGDMPfixture"
