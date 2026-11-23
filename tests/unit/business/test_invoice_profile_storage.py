"""The additive configuration schema is frozen and cannot allocate or issue numbers."""

import ast
from pathlib import Path
from typing import get_args

from coinpup_api.business.invoice_profile_models import InvoiceProfile
from coinpup_api.files.models import StoredFile
from coinpup_api.sync.schemas import EntityType
from sqlalchemy.dialects.postgresql import dialect
from sqlalchemy.schema import CreateIndex, CreateTable

from tests.integration.conftest import document_accrual_member_migration, invoice_profile_migration


def test_frozen_profile_schema_matches_scoped_model_and_adds_one_notification_type():
    current = invoice_profile_migration()
    previous = document_accrual_member_migration()
    assert current["revision"] == "20261113_0097"
    assert current["down_revision"] == "20261120_0132"
    assert current["_OLD_ENTITY_TYPES"] == previous["_OLD_ENTITY_TYPES"] + previous["_TABLES"]
    expected = current["_OLD_ENTITY_TYPES"] + current["_TABLES"]
    assert tuple(get_args(EntityType))[: len(expected)] == expected
    table = InvoiceProfile.__table__
    assert table.c.logo_file_id.references(StoredFile.__table__.c.id)
    assert current["_TABLES"] == (table.name,)
    for statement in (CreateTable(table), *(CreateIndex(i) for i in table.indexes)):
        assert " ".join(str(statement.compile(dialect=dialect())).split()) in " ".join(
            current["_SCHEMA_SQL"].split()
        )
    assert not {"number", "sequence", "next_sequence", "document_id", "response"} & set(
        table.c.keys()
    )
    path = Path("services/api/migrations/versions/20261113_0097_invoice_profiles.py")
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.ImportFrom):
            assert node.module == "alembic"
        assert not isinstance(node, ast.Import)
