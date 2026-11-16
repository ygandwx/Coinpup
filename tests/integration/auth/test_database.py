"""Run only against a disposable DB, after `alembic upgrade head`."""

import os
from datetime import UTC, date, datetime

import pytest
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from coinpup_api.config import Settings
from coinpup_api.database import Database
from coinpup_api.main import create_app
from fastapi.testclient import TestClient

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("COINPUP_RUN_DB_TESTS") != "1",
        reason="Set COINPUP_RUN_DB_TESTS=1 for an explicitly configured disposable PostgreSQL DB",
    ),
]


def test_migrated_postgresql_and_readiness(monkeypatch):
    monkeypatch.setenv("PGDATESTYLE", "German, DMY")
    settings = Settings()
    database = Database(settings)
    with TestClient(create_app(settings, database)) as client:
        with database.engine.connect() as connection:
            assert connection.exec_driver_sql(
                "SELECT DATE '2026-11-03', TIMESTAMPTZ '2026-11-03 12:34:56+00'"
            ).one() == (date(2026, 11, 3), datetime(2026, 11, 3, 12, 34, 56, tzinfo=UTC))
            assert (
                connection.exec_driver_sql("SELECT (DATE '2026-11-03')::text").scalar_one()
                == "2026-11-03"
            )
            heads = MigrationContext.configure(connection).get_current_heads()
            assert set(heads) == set(ScriptDirectory.from_config(Config("alembic.ini")).get_heads())
        assert client.get("/api/v1/health/ready").status_code == 200
