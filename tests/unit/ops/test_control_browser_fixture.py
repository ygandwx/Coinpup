"""Control browser fixtures fail closed before any database access."""

import importlib
from pathlib import Path

import pytest
from coinpup_api.config import Settings
from pydantic import SecretStr


@pytest.mark.parametrize(
    "opt_in,environment,host,name,allowed",
    [
        ("1", "test", "postgres", "coinpup", True),
        ("0", "test", "postgres", "coinpup", False),
        ("1", "production", "postgres", "coinpup", False),
        ("1", "test", "localhost", "coinpup", False),
        ("1", "test", "postgres", "fictional_other", False),
    ],
)
def test_browser_control_guard_precedes_engine(
    monkeypatch, opt_in, environment, host, name, allowed
):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[3] / "scripts"))
    fixture = importlib.import_module("create_control_browser_fixture")
    settings = Settings(_env_file=None).model_copy(
        update={
            "environment": environment,
            "database_url": SecretStr(f"postgresql+psycopg://fictional@{host}/{name}"),
        }
    )
    monkeypatch.setenv("COINPUP_CREATE_CONTROL_BROWSER_FIXTURE", opt_in)
    monkeypatch.setattr(fixture, "Settings", lambda: settings)

    def database(_settings):
        assert allowed, "Opened forbidden fixture engine"
        raise RuntimeError("Reached permitted fixture engine")

    monkeypatch.setattr(fixture, "Database", database)
    with pytest.raises(RuntimeError if allowed else SystemExit, match="fixture|Requires"):
        fixture.main()
