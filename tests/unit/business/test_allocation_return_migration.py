"""The additive storage migration is frozen, isolated and a single forward chain."""

import ast
import runpy
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory


def test_return_storage_keeps_previous_notification_types_and_frozen_migration_imports():
    root = Path(__file__).resolve().parents[3]
    versions = root / "services/api/migrations/versions"
    path = versions / "20261014_0033_allocation_return_storage.py"
    current = runpy.run_path(str(path))
    previous = runpy.run_path(str(versions / "20261012_0029_business_settlements.py"))
    assert current["_OLD_ENTITY_TYPES"] == previous["_OLD_ENTITY_TYPES"] + previous["_TABLES"]
    assert len(set(current["_OLD_ENTITY_TYPES"] + current["_TABLES"])) == 32
    assert all(len(value) <= 32 for value in current["_TABLES"])
    scripts = ScriptDirectory.from_config(Config(str(root / "alembic.ini")))
    assert len(scripts.get_heads()) == 1
    assert scripts.get_revision("20261014_0033").down_revision == "20261013_0032"
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.ImportFrom):
            assert node.module == "alembic"
        elif isinstance(node, ast.Import):
            raise AssertionError("Frozen storage SQL must not import runtime modules")
