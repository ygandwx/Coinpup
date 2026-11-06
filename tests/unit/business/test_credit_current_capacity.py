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
    for target, count in (("ALLOCATION", 1), ("CREDIT", 3)):
        old, current = migration["_OLD_" + target], migration["_" + target]
        assert old.count(migration["_" + target + "_SOURCE"]) == count
        assert current.count(migration["_" + target + "_CURRENT"]) == count
        assert (
            current.replace(
                migration["_" + target + "_CURRENT"], migration["_" + target + "_SOURCE"]
            )
            == old
        )
    assert migration["down_revision"] == "20261106_0045"
    assert "item.operation_version IS DISTINCT FROM 1" in migration["_CREDIT"]
    assert "item.status IS DISTINCT FROM 'active'" in migration["_CREDIT"]
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.ImportFrom):
            assert node.module == "alembic"
        assert not isinstance(node, ast.Import)
    assert "coinpup_validate_credit_revision(" not in path.read_text(encoding="utf-8")
