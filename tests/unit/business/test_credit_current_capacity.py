"""The closed-capacity layer changes only the canonical source of three aggregate totals."""

import ast
import runpy
from pathlib import Path


def test_current_capacity_keeps_exact_frozen_shape_and_refund_checks():
    versions = Path(__file__).resolve().parents[3] / "services/api/migrations/versions"
    path = versions / "20261106_0046_credit_current_capacity.py"
    migration = runpy.run_path(str(path))
    allocation = runpy.run_path(str(versions / "20261021_0038_business_credit_capacity.py"))
    credit = runpy.run_path(str(versions / "20261021_0039_business_credit_validation.py"))
    assert migration["_OLD_ALLOCATION"] == allocation["_ALLOCATION"]
    assert (
        migration["_OLD_CREDIT"]
        == credit["_VALIDATION"].split("CREATE FUNCTION coinpup_validate_shape_credit")[0]
    )
    for target in ("ALLOCATION", "CREDIT"):
        old, current = migration["_OLD_" + target], migration["_" + target]
        assert current.count("public.coinpup_current_credit_lines(") == 1
        assert "WITH credit_totals AS MATERIALIZED (" in current
        assert "GROUP BY c.source_line_id" in current
        assert "LEFT JOIN credit_totals credits" in current
        assert "credits.total_amount AS current_credit_total" in current
        assert "ARRAY[item." not in current
        for source, replacement in reversed(migration["_" + target + "_REPLACEMENTS"]):
            assert current.count(replacement) == 1
            current = current.replace(replacement, source)
        assert current == old  # All old IF positions, errors, refund and NULL semantics are frozen.
    assert "coalesce(item.current_credit_total,0)" in migration["_ALLOCATION"]
    for component in ("net", "tax", "total"):
        assert f"credits.{component}_amount AS current_credit_{component}" in migration["_CREDIT"]
        assert f"coalesce(item.current_credit_{component}" not in migration["_CREDIT"]
    assert migration["down_revision"] == "20261106_0045"
    assert "item.operation_version IS DISTINCT FROM 1" in migration["_CREDIT"]
    assert "item.status IS DISTINCT FROM 'active'" in migration["_CREDIT"]
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.ImportFrom):
            assert node.module == "alembic"
        assert not isinstance(node, ast.Import)
    assert "coinpup_validate_credit_revision(" not in path.read_text(encoding="utf-8")
