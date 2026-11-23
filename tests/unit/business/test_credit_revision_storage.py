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
        | {
            "business_credit_revision_members",
            "business_refund_revisions",
            "business_refund_revision_returns",
            "business_return_revisions",
            "business_return_revision_members",
            "business_document_revisions",
            "business_document_revision_lines",
            "business_document_accruals",
            "business_invoice_profiles",
        }
    )
    assert len(set(get_args(EntityType))) == 46
    table = BusinessCreditRevision.__table__
    original_fks = [
        fk
        for fk in table.foreign_key_constraints
        if fk.name != "fk_credit_revision_document_revision"
    ]
    generated = " ".join(
        str(
            CreateTable(table, include_foreign_key_constraints=original_fks).compile(
                dialect=dialect()
            )
        ).split()
    )
    # 0088 adds this column and FK without rewriting 0040's frozen header schema.
    assert "document_revision_id UUID, " in generated
    generated = generated.replace("document_revision_id UUID, ", "")
    assert generated in " ".join(migration["_SCHEMA_SQL"].split())
    assert len(table.name) <= 32
    assert all(len(constraint.name) <= 63 for constraint in table.constraints if constraint.name)
    assert {index.name for index in table.indexes} == {"ix_credit_revision_document_revision"}
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
    assert scripts.get_heads() == ["20261113_0097"]
    assert scripts.get_revision("20261106_0040").down_revision == "20261021_0039"
