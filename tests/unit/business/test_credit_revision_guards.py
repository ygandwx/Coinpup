"""The additive trigger layer cannot replace frozen financial validation or open its gate."""

import ast
import runpy
from pathlib import Path


def test_guard_layer_adds_only_two_triggers_and_preserves_the_closed_financial_call_graph():
    path = (
        Path(__file__).resolve().parents[3]
        / "services/api/migrations/versions/20261106_0047_credit_revision_guards.py"
    )
    source = path.read_text(encoding="utf-8")
    migration = runpy.run_path(str(path))
    assert migration["down_revision"] == "20261106_0046"
    assert source.count("CREATE FUNCTION ") == 2 and "CREATE OR REPLACE" not in source
    assert "coinpup_validate_credit_revision(" not in source
    assert "coinpup_validate_operation_history(" not in source
    assert "SELECT * INTO operation FROM public.financial_operations WHERE id=NEW.id" in source
    assert "NEW.version" not in migration["_TRIGGERS"]
    record = migration["_TRIGGERS"].split("CREATE FUNCTION coinpup_record_credit_group_change", 1)[
        1
    ]
    assert record.index("pg_advisory_xact_lock") < record.index("INSERT INTO public.change_log")
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.ImportFrom):
            assert node.module == "alembic"
        assert not isinstance(node, ast.Import)
