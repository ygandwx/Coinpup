"""Prepared facts do not replace a financial validator or open a write path."""

import ast
import runpy
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory


def test_fact_checks_are_frozen_leaf_functions_and_keep_the_single_migration_chain():
    root = Path(__file__).resolve().parents[3]
    path = root / "services/api/migrations/versions/20261106_0043_credit_fact_validation.py"
    source = path.read_text(encoding="utf-8")
    migration = runpy.run_path(str(path))
    assert migration["down_revision"] == "20261106_0042"
    assert source.count("CREATE FUNCTION ") == 2
    assert "CREATE OR REPLACE" not in source and "CREATE TRIGGER" not in source
    for sql in (migration["_POSTING"], migration["_ORIGIN"]):
        assert "SECURITY INVOKER SET search_path=pg_catalog" in sql
        assert not any(word in sql for word in ("UPDATE ", "DELETE ", "INSERT ", "FOR UPDATE"))
        assert "coinpup_validate_operation_history" not in sql
        assert "coinpup_validate_business_credit" not in sql
        assert "coinpup_validate_posting_shape" not in sql
        assert "coinpup_validate_credit_revision" not in sql
    assert "operation.current_journal_id" not in migration["_POSTING"]
    assert "operation.status" not in migration["_POSTING"]
    assert "actual_common := public.coinpup_validate_journal_common" in migration["_POSTING"]
    assert "common_result IS DISTINCT FROM actual_common" in migration["_POSTING"]
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.ImportFrom):
            assert node.module == "alembic"
        assert not isinstance(node, ast.Import)
    assert ScriptDirectory.from_config(Config(str(root / "alembic.ini"))).get_heads() == [
        "20261106_0048"
    ]
