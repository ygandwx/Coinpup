"""Shared, deliberately narrow PostgreSQL archive operations (POSIX runtimes)."""

import hashlib
import json
import os
import re
import stat
import subprocess
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

import psycopg
from sqlalchemy.engine import make_url


class ArchiveError(Exception):
    """A safe-to-display operational error; never includes connection details."""


@dataclass(frozen=True)
class DatabaseTarget:
    host: str
    port: int
    user: str
    database: str
    password: str = field(repr=False)
    sslmode: str = "prefer"

    def connect(self, *, autocommit=False):
        # These are single-process CLI helpers, not an application connection pool. Clear
        # libpq defaults while connecting so PGHOSTADDR/PGSERVICE cannot redirect preflight.
        inherited = {key: value for key, value in os.environ.items() if key.startswith("PG")}
        try:
            for key in inherited:
                del os.environ[key]
            return psycopg.connect(
                host=self.host,
                port=self.port,
                user=self.user,
                dbname=self.database,
                password=self.password,
                sslmode=self.sslmode,
                connect_timeout=10,
                application_name="coinpup_archive",
                options="-c statement_timeout=10000 -c lock_timeout=5000",
                autocommit=autocommit,
            )
        finally:
            os.environ.update(inherited)


def require_posix():
    if os.name != "posix":
        raise ArchiveError("Run archive commands in a Linux/POSIX host or Linux container.")


def read_target(variable: str) -> DatabaseTarget:
    value = os.environ.get(variable)
    if not value:
        raise ArchiveError(f"Set {variable} explicitly; .env is not loaded by archive commands.")
    try:
        url = make_url(value)
        if (
            url.drivername not in {"postgresql", "postgresql+psycopg"}
            or not url.host
            or not url.username
            or not url.database
            or not url.password
            or set(url.query) - {"sslmode"}
        ):
            raise ValueError
        sslmode = url.query.get("sslmode", "prefer")
        if sslmode not in {"disable", "allow", "prefer", "require", "verify-ca", "verify-full"}:
            raise ValueError
        parts = (url.host, url.username, url.database, url.password)
        if any(any(character in part for character in "\r\n\x00") for part in parts):
            raise ValueError
        return DatabaseTarget(
            url.host, url.port or 5432, url.username, url.database, url.password, sslmode
        )
    except Exception:
        raise ArchiveError(
            f"{variable} requires a PostgreSQL URL with host, user, password and database; "
            "only the sslmode query option is supported."
        ) from None


def create_private_directory(path: Path):
    require_posix()
    # mkdir is atomic and refuses an existing path, including a symlink.
    path.mkdir(mode=0o700)
    path.chmod(0o700)


def private_file(path: Path):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    return os.fdopen(descriptor, "wb")


@contextmanager
def tool_environment(target: DatabaseTarget):
    require_posix()
    with tempfile.TemporaryDirectory(prefix="coinpup-pg-") as temporary:
        directory = Path(temporary)
        directory.chmod(0o700)
        passfile = directory / "pgpass"
        password = target.password.replace("\\", "\\\\").replace(":", "\\:")
        with private_file(passfile) as output:
            output.write(f"*:*:*:*:{password}\n".encode())
        # Ignore inherited libpq defaults and do not copy application credentials to child tools.
        environment = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith(("PG", "COINPUP_", "POSTGRES_"))
        }
        environment.update(
            PGHOST=target.host,
            PGPORT=str(target.port),
            PGUSER=target.user,
            PGDATABASE=target.database,
            PGPASSFILE=str(passfile),
            PGSSLMODE=target.sslmode,
            PGCONNECT_TIMEOUT="10",
            PGAPPNAME="coinpup_archive",
            PGOPTIONS="-c lock_timeout=10000",
        )
        yield environment


def run_tool(arguments, *, environment, stdout=None, stdin=None):
    try:
        result = subprocess.run(
            arguments,
            env=environment,
            stdin=stdin if stdin is not None else subprocess.DEVNULL,
            stdout=stdout if stdout is not None else subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    except OSError:
        raise ArchiveError(
            f"{arguments[0]} could not be started; check PostgreSQL client tools."
        ) from None
    if result.returncode != 0:
        raise ArchiveError(
            f"{arguments[0]} failed (exit {result.returncode}); no success was recorded. "
            "Tool diagnostics are suppressed to keep database values and credentials private."
        )


def digest_file(source):
    source.seek(0)
    digest = hashlib.sha256()
    size = 0
    while block := source.read(1024 * 1024):
        digest.update(block)
        size += len(block)
    source.seek(0)
    return digest.hexdigest(), size


def validate_disposable_target(target: DatabaseTarget, confirmation: str):
    if (
        confirmation != target.database
        or re.fullmatch(r"coinpup_restore_[a-z0-9_]+", target.database) is None
    ):
        raise ArchiveError(
            "Restore requires a new database named coinpup_restore_<suffix> and "
            "--confirm-empty-database with that exact database name."
        )


EMPTY_DATABASE_QUERY = """
SELECT EXISTS (
    SELECT 1 FROM pg_catalog.pg_namespace
      WHERE nspname NOT IN ('public', 'information_schema') AND left(nspname, 3) <> 'pg_'
    UNION ALL
    SELECT 1 FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
      WHERE n.nspname <> 'information_schema' AND left(n.nspname, 3) <> 'pg_'
    UNION ALL
    SELECT 1 FROM pg_catalog.pg_proc p JOIN pg_catalog.pg_namespace n ON n.oid = p.pronamespace
      WHERE n.nspname <> 'information_schema' AND left(n.nspname, 3) <> 'pg_'
    UNION ALL
    SELECT 1 FROM pg_catalog.pg_type t JOIN pg_catalog.pg_namespace n ON n.oid = t.typnamespace
      WHERE n.nspname <> 'information_schema' AND left(n.nspname, 3) <> 'pg_'
    UNION ALL
    SELECT 1 FROM pg_catalog.pg_extension WHERE extname <> 'plpgsql'
    UNION ALL
    SELECT 1 FROM pg_catalog.pg_largeobject_metadata
    UNION ALL
    SELECT 1 FROM pg_catalog.pg_event_trigger
    UNION ALL
    SELECT 1 FROM pg_catalog.pg_publication
    UNION ALL
    SELECT 1 FROM pg_catalog.pg_subscription WHERE subdbid = (SELECT oid FROM pg_catalog.pg_database
      WHERE datname = current_database())
    UNION ALL
    SELECT 1 FROM pg_catalog.pg_collation c
      JOIN pg_catalog.pg_namespace n ON n.oid = c.collnamespace
      WHERE n.nspname <> 'information_schema' AND left(n.nspname, 3) <> 'pg_'
    UNION ALL
    SELECT 1 FROM pg_catalog.pg_foreign_server
    UNION ALL
    SELECT 1 FROM pg_catalog.pg_foreign_data_wrapper
)
"""


@contextmanager
def empty_target_guard(target: DatabaseTarget):
    try:
        with target.connect(autocommit=True) as connection:
            # Serializes Coinpup restore attempts; unrelated writers must remain disconnected.
            locked = connection.execute("SELECT pg_try_advisory_lock(6842542789920341)").fetchone()[
                0
            ]
            if not locked:
                raise ArchiveError("Another Coinpup restore is using the target database.")
            actual_name = connection.execute("SELECT current_database()").fetchone()[0]
            if actual_name != target.database:
                raise ArchiveError("Connected database does not match the confirmed target.")
            if connection.execute(EMPTY_DATABASE_QUERY).fetchone()[0]:
                raise ArchiveError("Target database is not empty; nothing was restored or removed.")
            if connection.execute(
                "SELECT EXISTS (SELECT 1 FROM pg_catalog.pg_stat_activity "
                "WHERE datname = current_database() AND pid <> pg_backend_pid())"
            ).fetchone()[0]:
                raise ArchiveError(
                    "Target has other connections; disconnect them before restoring."
                )
            yield
    except psycopg.Error:
        raise ArchiveError(
            "Target database checks failed; no restore success was recorded."
        ) from None


@contextmanager
def verified_archive(directory: Path):
    # Hold the verified file descriptor through pg_restore so a pathname replacement cannot
    # substitute a different archive between the checksum and restore operations.
    if directory.is_symlink() or not directory.is_dir():
        raise ArchiveError("Backup must be a real directory containing a completed manifest.")
    manifest_path = directory / "manifest.json"
    dump_path = directory / "database.dump"
    if manifest_path.is_symlink() or dump_path.is_symlink():
        raise ArchiveError("Backup files must not be symbolic links.")
    try:
        if manifest_path.stat().st_size > 16384:
            raise ValueError
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if (
            manifest.get("version") != 1
            or manifest.get("format") != "postgresql-custom"
            or manifest.get("file") != "database.dump"
            or manifest.get("scope") != "database-only"
            or not isinstance(manifest.get("sha256"), str)
            or re.fullmatch(r"[a-f0-9]{64}", manifest["sha256"]) is None
            or type(manifest.get("bytes")) is not int
        ):
            raise ValueError
        with dump_path.open("rb") as source:
            if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
                raise ValueError
            if source.read(5) != b"PGDMP":
                raise ValueError
            digest, size = digest_file(source)
            if digest != manifest["sha256"] or size != manifest["bytes"]:
                raise ValueError
            yield source
    except (OSError, ValueError, TypeError, AttributeError):
        raise ArchiveError("Backup is incomplete, invalid or has a SHA-256 mismatch.") from None
