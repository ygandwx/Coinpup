"""The additive storage migration is frozen, isolated and a single forward chain."""

import ast
import runpy
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory


def test_return_storage_and_validation_keep_frozen_migrations_and_one_forward_head():
    root = Path(__file__).resolve().parents[3]
    versions = root / "services/api/migrations/versions"
    path = versions / "20261014_0033_allocation_return_storage.py"
    current = runpy.run_path(str(path))
    validation_path = versions / "20261014_0034_allocation_return_validation.py"
    validation = runpy.run_path(str(validation_path))
    previous = runpy.run_path(str(versions / "20261012_0029_business_settlements.py"))
    assert current["_OLD_ENTITY_TYPES"] == previous["_OLD_ENTITY_TYPES"] + previous["_TABLES"]
    assert len(set(current["_OLD_ENTITY_TYPES"] + current["_TABLES"])) == 32
    assert all(len(value) <= 32 for value in current["_TABLES"])
    scripts = ScriptDirectory.from_config(Config(str(root / "alembic.ini")))
    assert len(scripts.get_heads()) == 1
    assert scripts.get_revision("20261014_0033").down_revision == "20261013_0032"
    assert scripts.get_revision("20261014_0034").down_revision == "20261014_0033"
    assert validation["_PREVIOUS_KINDS"] == (
        "kind IN ('opening', 'income', 'expense', 'transfer', 'exchange', "
        "'invoice_accrual', 'bill_accrual', 'payment', 'allocation')"
    )
    for migration_path in (path, validation_path):
        for node in ast.walk(ast.parse(migration_path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.ImportFrom):
                assert node.module == "alembic"
            elif isinstance(node, ast.Import):
                raise AssertionError("Frozen SQL migrations must not import runtime modules")
