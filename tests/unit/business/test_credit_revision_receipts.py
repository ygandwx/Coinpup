"""The response proof stays additive and cannot claim to reconstruct unstored raw intent."""

import ast
import runpy
from pathlib import Path


def test_revision_receipt_helpers_are_frozen_read_only_and_keep_existing_callers():
    path = Path(__file__).resolve().parents[3] / (
        "services/api/migrations/versions/20261106_0045_credit_revision_receipts.py"
    )
    source = path.read_text(encoding="utf-8")
    frozen = runpy.run_path(str(path))
    assert frozen["down_revision"] == "20261106_0044"
    assert source.count("CREATE FUNCTION ") == 2
    assert "CREATE OR REPLACE" not in source and "CREATE TRIGGER" not in source
    sql = frozen["_RESPONSE"] + frozen["_RECEIPT"]
    for forbidden in (
        "INSERT ",
        "UPDATE ",
        "DELETE ",
        "FOR UPDATE",
        "request_hash",
        "digest(",
        "coinpup_validate_credit_revision(",
        "coinpup_validate_credit_group_transitions(",
        "coinpup_business_credit_response(",
        "coinpup_current_credit_lines(",
        "coinpup_validate_operation_history(",
        "coinpup_record_change(",
    ):
        assert forbidden not in sql
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.ImportFrom):
            assert node.module == "alembic"
        assert not isinstance(node, ast.Import)
