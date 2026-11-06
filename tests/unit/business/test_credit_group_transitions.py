"""A frozen topology proof cannot become an alternate financial writer or recursive validator."""

import ast
import runpy
from pathlib import Path


def test_group_proof_is_additive_read_only_and_does_not_open_the_gate():
    path = Path(__file__).resolve().parents[3] / (
        "services/api/migrations/versions/20261106_0044_credit_group_transitions.py"
    )
    source = path.read_text(encoding="utf-8")
    frozen = runpy.run_path(str(path))
    assert frozen["down_revision"] == "20261106_0043"
    assert source.count("CREATE FUNCTION ") == 1
    assert "CREATE OR REPLACE" not in source and "CREATE TRIGGER" not in source
    sql = frozen["_TRANSITIONS"]
    for forbidden in (
        "INSERT ",
        "UPDATE ",
        "DELETE ",
        "FOR UPDATE",
        "coinpup_validate_operation_history(",
        "coinpup_validate_posting_shape(",
        "coinpup_validate_credit_revision(",
        "coinpup_record_change(",
        "command_receipts",
    ):
        assert forbidden not in sql
    assert "public.coinpup_credit_posting_facts(" in sql
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.ImportFrom):
            assert node.module == "alembic"
        assert not isinstance(node, ast.Import)
