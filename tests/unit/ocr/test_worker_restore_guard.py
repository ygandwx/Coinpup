"""The destructive fixture workflow must reject unacknowledged or unrelated databases."""

import importlib
import sys
from pathlib import Path

import pytest


@pytest.fixture
def checker(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[3] / "scripts"))
    return importlib.import_module("check_ocr_restore")


@pytest.mark.parametrize(
    "opt_in,source,target",
    [
        ("0", "coinpup_test_worker", "coinpup_restore_worker"),
        ("true", "coinpup_test_worker", "coinpup_restore_worker"),
        ("1", "fictional_wrong_source", "coinpup_restore_worker"),
        ("1", "coinpup_test_worker", "fictional_wrong_target"),
        ("1", "coinpup_restore_worker", "coinpup_test_worker"),
        ("1", "coinpup_test_worker", "coinpup_test_worker"),
    ],
)
def test_fixture_guard_precedes_engines_and_files(checker, monkeypatch, opt_in, source, target):
    monkeypatch.setenv("COINPUP_RUN_WORKER_RESTORE", opt_in)
    for variable, name in (
        ("COINPUP_BACKUP_DATABASE_URL", source),
        ("COINPUP_RESTORE_DATABASE_URL", target),
    ):
        monkeypatch.setenv(variable, f"postgresql+psycopg://fictional:fake@localhost/{name}")
    monkeypatch.setattr(sys, "argv", ["check_ocr_restore", "bundle"])
    monkeypatch.setattr(checker, "database", lambda *_: pytest.fail("Opened forbidden engine"))
    monkeypatch.setattr(checker, "bundle", lambda *_: pytest.fail("Touched private files"))
    with pytest.raises(ValueError, match="Requires"):
        checker.main()
