"""Run only against a disposable DB, after `alembic upgrade head`."""

import os
import subprocess
import sys
from datetime import UTC, date, datetime
from functools import cache
from pathlib import Path
from uuid import uuid4

import pytest
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from coinpup_api import private_fields
from coinpup_api.config import Settings
from coinpup_api.database import Database
from coinpup_api.ledger.schemas import AccountCreate, AccountUpdate, EntityCreate
from coinpup_api.ledger.service import LedgerService
from coinpup_api.main import create_app
from coinpup_private_v1 import PrivateFieldError, load_key
from fastapi.testclient import TestClient
from sqlalchemy import text

from scripts.init_private_key import generate

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


def test_new_api_process_rejects_missing_and_incorrect_key_before_serving(tmp_path):
    # A new worker must read its own mounted key, even if the parent process is already serving.
    assert os.environ["COINPUP_DATABASE_URL"]
    api_source = Path(__file__).resolve().parents[3] / "services/api/src"
    env = os.environ | {"PYTHONPATH": str(api_source)}
    probe = [
        sys.executable,
        "-c",
        "from coinpup_api.main import create_app\n"
        "from fastapi.testclient import TestClient\n"
        "with TestClient(create_app()) as client:\n"
        "    client.get('/api/v1/health/ready').raise_for_status()\n",
    ]
    control = subprocess.run(probe, env=env, capture_output=True, timeout=30)
    assert control.returncode == 0
    wrong = tmp_path / "fictional-wrong.key"
    generate(wrong, load_key().id)
    for path in (tmp_path / "missing.key", wrong):
        result = subprocess.run(
            probe,
            env=env | {"COINPUP_PRIVATE_KEY_FILE": str(path)},
            capture_output=True,
            timeout=30,
        )
        assert result.returncode != 0
        assert b"Private account storage is unavailable" in result.stderr
        assert str(path).encode() not in result.stderr


def test_direct_account_writes_verify_database_key_in_transaction(
    structure_database, tmp_path, monkeypatch
):
    engine, owner = structure_database
    service = LedgerService(engine)
    ledger = service.create_entity(
        owner,
        EntityCreate(
            kind="personal",
            name="Fictional key check",
            base_asset_id="USD",
            template_key="personal_default",
        ),
    ).ledger.id
    account = service.create_account(
        owner, ledger, AccountCreate(name="Fictional bank", kind="bank", asset_ids=["USD"])
    )

    def snapshot():
        with engine.connect() as db:
            return (
                db.execute(text("SELECT to_jsonb(t) FROM accounts t ORDER BY id")).all(),
                db.scalar(text("SELECT count(*) FROM change_log")),
            )

    before = snapshot()
    wrong_key = tmp_path / "fictional-second-worker.key"
    generate(wrong_key, load_key().id)
    monkeypatch.setenv("COINPUP_PRIVATE_KEY_FILE", str(wrong_key))
    # Model a second service process with an empty key cache, calling without the HTTP lifespan.
    monkeypatch.setattr(private_fields, "_runtime_key", cache(load_key))
    details = {"account_number": uuid4().hex}
    with pytest.raises(PrivateFieldError):
        service.create_account(
            owner,
            ledger,
            AccountCreate(name="Fictional bank", kind="bank", asset_ids=["USD"], details=details),
        )
    with pytest.raises(PrivateFieldError):
        service.update_account(
            owner,
            ledger,
            account.id,
            AccountUpdate(expected_version=account.version, details=details),
        )
    assert snapshot() == before
