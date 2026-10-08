"""Explicit enablement freezes server policy without changing stored-intent overrides."""

import pytest
from coinpup_api import main
from coinpup_api.config import Settings
from coinpup_api.ocr.runtime import processing_configuration
from fastapi import APIRouter

from tests.unit.ocr.test_ocr_router import Probe


@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("override", [None, {}])
def test_explicit_ocr_enablement_preserves_injected_configuration(enabled, override, monkeypatch):
    captured = []

    def router(settings, engine, configuration):
        captured.append(configuration)
        return APIRouter()

    monkeypatch.setattr(main, "create_ocr_router", router)
    main.create_app(
        Settings(_env_file=None, environment="test", ocr_enabled=enabled),
        Probe(),
        ocr_configuration=override,
    )
    expected = (
        {"lease_seconds": 120, "retry_seconds": 30, "processing": processing_configuration()}
        if enabled and override is None
        else override
    )
    assert captured == [expected]


def test_ocr_new_jobs_are_disabled_by_default():
    assert Settings(_env_file=None).ocr_enabled is False


@pytest.mark.parametrize(
    "opt_in,name", [("0", "coinpup_test_worker"), ("1", "fictional_other_database")]
)
def test_container_seed_requires_explicit_disposable_database(opt_in, name, monkeypatch):
    from scripts import check_ocr_worker

    monkeypatch.setenv("COINPUP_RUN_WORKER_SMOKE", opt_in)
    monkeypatch.setenv(
        "COINPUP_DATABASE_URL", f"postgresql+psycopg://fictional@localhost:5432/{name}"
    )
    monkeypatch.setattr(
        check_ocr_worker, "Database", lambda *_: pytest.fail("Opened forbidden database")
    )
    with pytest.raises(ValueError, match="explicitly opted-in"):
        check_ocr_worker.database()
