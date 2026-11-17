"""The new head reproduces exactly the ORM table and the full additive sync catalog."""

import runpy
from pathlib import Path
from typing import get_args

from coinpup_api.business.document_revision_models import BusinessDocumentRevision
from coinpup_api.ledger import models as ledger_models  # noqa: F401
from coinpup_api.sync.schemas import EntityType
from sqlalchemy.dialects.postgresql import dialect
from sqlalchemy.schema import CreateTable


def test_confirmed_revision_header_relations_and_previous_catalog_match_the_ddl():
    versions = Path(__file__).resolve().parents[3] / "services/api/migrations/versions"
    previous = runpy.run_path(str(versions / "20261109_0061_allocation_return_revision_members.py"))
    current = runpy.run_path(str(versions / "20261110_0075_confirmed_document_revision_headers.py"))
    table = BusinessDocumentRevision.__table__
    assert current["down_revision"] == "20261109_0074"
    assert current["_OLD_ENTITY_TYPES"] == previous["_OLD_ENTITY_TYPES"] + previous["_TABLES"]
    assert current["_TABLES"] == (table.name,) and len(table.name) <= 32
    assert tuple(get_args(EntityType)) == current["_OLD_ENTITY_TYPES"] + current["_TABLES"] + (
        "business_document_revision_lines",
    )
    sql = str(CreateTable(table).compile(dialect=dialect()))
    assert " ".join(sql.split()) in " ".join(current["_SCHEMA_SQL"].split())


def test_document_line_ddl_matches_composite_fks_and_current_additive_catalog():
    from coinpup_api.business.document_revision_models import BusinessDocumentRevisionLine

    versions = Path(__file__).resolve().parents[3] / "services/api/migrations/versions"
    previous = runpy.run_path(
        str(versions / "20261110_0075_confirmed_document_revision_headers.py")
    )
    current = runpy.run_path(str(versions / "20261110_0076_confirmed_document_revision_lines.py"))
    table = BusinessDocumentRevisionLine.__table__
    assert current["down_revision"] == previous["revision"]
    assert current["_OLD_ENTITY_TYPES"] == previous["_OLD_ENTITY_TYPES"] + previous["_TABLES"]
    assert current["_TABLES"] == (table.name,) and len(table.name) <= 32
    assert tuple(get_args(EntityType)) == current["_OLD_ENTITY_TYPES"] + current["_TABLES"]
    sql = str(CreateTable(table).compile(dialect=dialect()))
    assert " ".join(sql.split()) in " ".join(current["_SCHEMA_SQL"].split())
