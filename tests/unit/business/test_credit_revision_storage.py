"""The gated revision header is additive and its DDL is frozen independently of runtime code."""

import ast
import runpy
from pathlib import Path
from typing import get_args

from alembic.config import Config
from alembic.script import ScriptDirectory
from coinpup_api.business.credit_revision_models import BusinessCreditRevision
from coinpup_api.ledger import models as ledger_models  # noqa: F401
from coinpup_api.sync.schemas import EntityType
from sqlalchemy.dialects.postgresql import dialect
from sqlalchemy.schema import CreateTable


def test_revision_header_ddl_matches_model_and_only_extends_the_current_catalog():
    root = Path(__file__).resolve().parents[3]
    versions = root / "services/api/migrations/versions"
    path = versions / "20261106_0040_credit_revision_headers.py"
    migration = runpy.run_path(str(path))
    previous = runpy.run_path(str(versions / "20261021_0037_business_credit_storage.py"))
    assert migration["_OLD_ENTITY_TYPES"] == previous["_OLD_ENTITY_TYPES"] + previous["_TABLES"]
    assert migration["_TABLES"] == ("business_credit_revisions",)
    assert set(get_args(EntityType)) == (
        set(migration["_OLD_ENTITY_TYPES"] + migration["_TABLES"])
        | {"business_credit_revision_members"}
    )
    assert len(set(get_args(EntityType))) == 38
    table = BusinessCreditRevision.__table__
    generated = str(CreateTable(table).compile(dialect=dialect()))
    assert " ".join(generated.split()) in " ".join(migration["_SCHEMA_SQL"].split())
    assert len(table.name) <= 32
    assert all(len(constraint.name) <= 63 for constraint in table.constraints if constraint.name)
    assert not table.indexes
    source = path.read_text(encoding="utf-8")
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.ImportFrom):
            assert node.module == "alembic"
        assert not isinstance(node, ast.Import)
    assert "CREATE OR REPLACE FUNCTION" not in source
    for unchanged in (
        "coinpup_validate_business_credit(",
        "coinpup_validate_operation_history(",
        "coinpup_validate_business_allocation(",
        "coinpup_validate_posting_shape(",
    ):
        assert unchanged not in source
    assert "complete group validation" in migration["_GUARDS"]
    scripts = ScriptDirectory.from_config(Config(str(root / "alembic.ini")))
    assert scripts.get_heads() == ["20261106_0048"]
    assert scripts.get_revision("20261106_0040").down_revision == "20261021_0039"
