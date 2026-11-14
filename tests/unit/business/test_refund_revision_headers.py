"""The new snapshot has frozen DDL, scoped references and one sync identity."""

import ast
import runpy
from pathlib import Path
from typing import get_args

from coinpup_api.business.refund_revision_models import BusinessRefundRevision
from coinpup_api.ledger import models as ledger_models  # noqa: F401
from coinpup_api.sync.schemas import EntityType
from sqlalchemy.dialects.postgresql import dialect
from sqlalchemy.schema import CreateTable


def test_refund_revision_migration_extends_the_catalog_with_one_gated_table():
    versions = Path(__file__).resolve().parents[3] / "services/api/migrations/versions"
    path = versions / "20261109_0049_refund_revision_headers.py"
    schema = runpy.run_path(str(path))
    previous = runpy.run_path(str(versions / "20261106_0041_credit_revision_members.py"))
    assert schema["down_revision"] == "20261106_0048"
    assert schema["_TABLES"] == ("business_refund_revisions",)
    assert schema["_OLD_ENTITY_TYPES"] == previous["_OLD_ENTITY_TYPES"] + previous["_TABLES"]
    assert set(get_args(EntityType)) == set(schema["_OLD_ENTITY_TYPES"] + schema["_TABLES"]) | {
        "business_refund_revision_returns"
    }
    table = BusinessRefundRevision.__table__
    sql = " ".join(str(CreateTable(table).compile(dialect=dialect())).split())
    assert sql in " ".join(schema["_SCHEMA_SQL"].split())
    for column in (table.c.amount, table.c.payment_amount):
        assert (column.type.precision, column.type.scale) == (38, 18)
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.ImportFrom):
            assert node.module == "alembic"
        assert not isinstance(node, ast.Import)
