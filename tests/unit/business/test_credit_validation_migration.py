"""Credit validation adds two kinds without replacing the established history chain."""

import ast
import runpy
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory


def test_credit_dispatch_and_empty_downgrade_preserve_all_previous_shapes():
    root = Path(__file__).resolve().parents[3]
    versions = root / "services/api/migrations/versions"
    path = versions / "20261021_0039_business_credit_validation.py"
    current = runpy.run_path(str(path))
    old = runpy.run_path(str(versions / "20261015_0036_business_refund_validation.py"))
    assert current["_OLD_DISPATCH"] == old["_DISPATCH"]
    assert current["_OLD_KINDS"] == old["_KINDS"]
    addition = (
        "        WHEN 'invoice_credit', 'bill_credit' THEN\n"
        "            PERFORM public.coinpup_validate_shape_credit("
        "journal_identifier, common_result);\n"
    )
    assert current["_DISPATCH"].replace(addition, "") == old["_DISPATCH"]
    assert current["_DISPATCH"].count(addition) == 1
    assert "coinpup_validate_operation_history" not in path.read_text()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.ImportFrom):
            assert node.module == "alembic"
        assert not isinstance(node, ast.Import)
    scripts = ScriptDirectory.from_config(Config(str(root / "alembic.ini")))
    assert scripts.get_heads() == ["20261106_0045"]
    assert scripts.get_revision("20261021_0039").down_revision == "20261021_0038"
