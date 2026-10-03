"""Opt-in CI exercise. Both URLs must point to disposable test databases; never drops a DB."""

import os
import secrets
import sys
import tempfile
from pathlib import Path

import psycopg
from _database_archive import ArchiveError, read_target, require_posix
from backup_database import backup_database
from psycopg import sql
from restore_database import restore_database


def snapshot(target):
    tables = {
        "administrators": "id",
        "auth_sessions": "token_hash",
        "auth_login_guard": "id",
        "alembic_version": "version_num",
    }
    with target.connect() as connection:
        return {
            table: connection.execute(
                sql.SQL("SELECT row_to_json(t) FROM {} t ORDER BY {}").format(
                    sql.Identifier(table), sql.Identifier(key)
                )
            ).fetchall()
            for table, key in tables.items()
        }


def check_backup_restore():
    require_posix()
    if os.environ.get("COINPUP_RUN_BACKUP_TESTS") != "1":
        raise ArchiveError("Set COINPUP_RUN_BACKUP_TESTS=1 only for disposable CI databases.")
    source = read_target("COINPUP_BACKUP_TEST_SOURCE_URL")
    target = read_target("COINPUP_BACKUP_TEST_TARGET_URL")
    if (
        target.database != "coinpup_restore_test"
        or source.database == target.database
        or (source.host, source.port, source.user) != (target.host, target.port, target.user)
    ):
        raise ArchiveError(
            "CI target must be coinpup_restore_test on the same test server and user."
        )
    # Refuse existing target and existing identity: never overwrite or reset either.
    with source.connect(autocommit=True) as connection:
        if connection.execute(
            "SELECT EXISTS (SELECT 1 FROM pg_database WHERE datname = %s)", (target.database,)
        ).fetchone()[0]:
            raise ArchiveError(
                "CI restore database already exists; use a fresh PostgreSQL service."
            )
        if connection.execute("SELECT EXISTS (SELECT 1 FROM administrators)").fetchone()[0]:
            raise ArchiveError(
                "CI source already has an administrator; use a fresh migrated source."
            )
        connection.execute(
            sql.SQL("CREATE DATABASE {} TEMPLATE template0").format(sql.Identifier(target.database))
        )

    from coinpup_api.admin import create_admin
    from coinpup_api.auth import AuthService
    from coinpup_api.config import Settings
    from sqlalchemy import create_engine

    source_url = os.environ["COINPUP_BACKUP_TEST_SOURCE_URL"]
    target_url = os.environ["COINPUP_BACKUP_TEST_TARGET_URL"]
    # Accept plain PostgreSQL URLs in CLI configuration, consistently use psycopg in SQLAlchemy.
    source_url = source_url.replace("postgresql://", "postgresql+psycopg://", 1)
    target_url = target_url.replace("postgresql://", "postgresql+psycopg://", 1)
    settings = Settings(environment="test", database_url=source_url, _env_file=None)
    source_engine = create_engine(source_url, hide_parameters=True)
    target_engine = create_engine(target_url, hide_parameters=True)
    try:
        password = secrets.token_urlsafe(32)
        administrator_id = create_admin(source_engine, "backup-ci-fixture", password)
        identity, token = AuthService(settings, source_engine).login("backup-ci-fixture", password)
        if identity.id != administrator_id:
            raise ArchiveError("CI fixture login did not match its administrator.")
        source_engine.dispose()
        before = snapshot(source)
        with tempfile.TemporaryDirectory(prefix="coinpup-backup-check-") as temporary:
            archive = Path(temporary) / "backup"
            backup_database(source, archive)
            restore_database(target, archive, target.database)
            try:
                restore_database(target, archive, target.database)
            except ArchiveError as error:
                if "not empty" not in str(error):
                    raise
            else:
                raise ArchiveError("Second restore should have refused the nonempty target.")
        if before != snapshot(source) or before != snapshot(target):
            raise ArchiveError("Source/restore authentication rows or migration revision differ.")
        for engine in (source_engine, target_engine):
            if AuthService(settings, engine).get_session(token).id != administrator_id:
                raise ArchiveError("Restored or original session is not usable.")
        print("Backup/restore verified: administrator, session, throttle and migration rows match.")
        print("Original and restored sessions both resolve. Test databases retained; no DROP ran.")
    finally:
        source_engine.dispose()
        target_engine.dispose()


def main():
    try:
        check_backup_restore()
    except ArchiveError as error:
        print(str(error), file=sys.stderr)
        return 1
    except (psycopg.Error, OSError):
        print(
            "Backup/restore CI database or filesystem check failed; details suppressed.",
            file=sys.stderr,
        )
        return 1
    except Exception:
        print(
            "Backup/restore CI authentication or comparison failed; details suppressed.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
