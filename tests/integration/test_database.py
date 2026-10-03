"""Run only against a disposable DB, after `alembic upgrade head`."""

import os

import pytest
from alembic.migration import MigrationContext
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


def test_migrated_postgresql_and_readiness():
    settings = Settings()
    database = Database(settings)
    with TestClient(create_app(settings, database)) as client:
        with database.engine.connect() as connection:
            heads = MigrationContext.configure(connection).get_current_heads()
            assert heads == ("20261003_0001",)
        assert client.get("/api/v1/health/ready").status_code == 200
