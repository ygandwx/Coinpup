"""Keep the exact deployed legacy guard and a single additive frozen migration."""

import ast
import runpy
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory


def test_payment_evidence_downgrade_preserves_frozen_0014_sql_and_single_head():
    root = Path(__file__).resolve().parents[3]
    versions = root / "services/api/migrations/versions"
    legacy = runpy.run_path(str(versions / "20261008_0014_ocr_confirmations.py"))
    path = versions / "20261013_0032_ocr_payment_evidence.py"
    current = runpy.run_path(str(path))
    assert current["_OLD_CONSISTENCY_SQL"] == legacy["_CONSISTENCY_SQL"]
    assert current["down_revision"] == "20261012_0031"
    scripts = ScriptDirectory.from_config(Config(str(root / "alembic.ini")))
    assert len(scripts.get_heads()) == 1
    assert scripts.get_revision("20261013_0032").down_revision == "20261012_0031"
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.ImportFrom):
            assert node.module.split(".")[0] in {"alembic", "sqlalchemy"}
        elif isinstance(node, ast.Import):
            assert all(item.name.split(".")[0] in {"alembic", "sqlalchemy"} for item in node.names)
