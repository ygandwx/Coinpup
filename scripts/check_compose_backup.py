"""Create isolated backup test databases on the disposable Compose PostgreSQL service."""

import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services/api/src"))

from check_backup_restore import check_backup_restore  # noqa: E402
from coinpup_api.config import Settings  # noqa: E402
from coinpup_api.database import Database  # noqa: E402
from sqlalchemy import text  # noqa: E402
from sqlalchemy.engine import make_url  # noqa: E402


def main():
    settings = Settings()
    if settings.environment != "test" or os.environ.get("COINPUP_RUN_BACKUP_TESTS") != "1":
        raise RuntimeError("This exercise requires explicit disposable test configuration")
    database = Database(settings)
    try:
        with database.engine.connect().execution_options(
            isolation_level="AUTOCOMMIT"
        ) as connection:
            # CREATE fails if this database exists. Never reuse, drop or replace a target.
            connection.execute(text("CREATE DATABASE coinpup_backup_test TEMPLATE template0"))
    finally:
        database.close()
    url = make_url(settings.database_url.get_secret_value())
    source = url.set(database="coinpup_backup_test").render_as_string(hide_password=False)
    target = url.set(database="coinpup_restore_test").render_as_string(hide_password=False)
    environment = dict(os.environ, COINPUP_DATABASE_URL=source)
    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"], env=environment, check=True
    )
    os.environ["COINPUP_BACKUP_TEST_SOURCE_URL"] = source
    os.environ["COINPUP_BACKUP_TEST_TARGET_URL"] = target
    check_backup_restore()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        raise SystemExit(
            "Compose backup exercise failed; test databases retained for inspection"
        ) from None
