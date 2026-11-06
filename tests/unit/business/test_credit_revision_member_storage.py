"""Frozen member DDL leaves the financial validators and closed header gate unchanged."""

import ast
import runpy
from pathlib import Path
from typing import get_args

from coinpup_api.business.credit_revision_member_models import BusinessCreditRevisionMember
from coinpup_api.ledger import models as ledger_models  # noqa: F401
from coinpup_api.sync.schemas import EntityType
from sqlalchemy.dialects.postgresql import dialect
from sqlalchemy.schema import CreateTable


def test_member_ddl_catalog_and_operation_history_keys():
    versions = Path(__file__).resolve().parents[3] / "services/api/migrations/versions"
    path = versions / "20261106_0041_credit_revision_members.py"
    migration = runpy.run_path(str(path))
    previous = runpy.run_path(str(versions / "20261106_0040_credit_revision_headers.py"))
    assert migration["down_revision"] == previous["revision"]
    assert migration["_OLD_ENTITY_TYPES"] == previous["_OLD_ENTITY_TYPES"] + previous["_TABLES"]
    assert set(get_args(EntityType)) == set(migration["_OLD_ENTITY_TYPES"] + migration["_TABLES"])
    assert len(get_args(EntityType)) == 38
    table = BusinessCreditRevisionMember.__table__
    assert " ".join(str(CreateTable(table).compile(dialect=dialect())).split()) in " ".join(
        migration["_SCHEMA_SQL"].split()
    )
    assert len(table.name) <= 32
    assert all(len(c.name) <= 63 for c in table.constraints if c.name)
    assert not table.indexes
    assert not any(set(c.columns.keys()) == {"operation_id"} for c in table.constraints)
    assert not any(name in table.c for name in ("previous_net_amount", "previous_recognition_date"))
    for column in ("net_amount", "tax_amount", "total_amount"):
        assert (table.c[column].type.precision, table.c[column].type.scale) == (38, 18)
    source = path.read_text(encoding="utf-8")
    assert "CREATE OR REPLACE FUNCTION" not in source
    assert source.count("CREATE FUNCTION ") == 1
    assert "coinpup_validate_credit_revision(NEW.revision_id)" in migration["_GUARDS"]
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.ImportFrom):
            assert node.module == "alembic"
        assert not isinstance(node, ast.Import)
