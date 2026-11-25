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
        "business_document_accruals",
        "business_invoice_profiles",
        "business_invoice_reservations",
        "business_invoice_completions",
        "interledger_settings",
        "interledger_groups",
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
    assert tuple(get_args(EntityType)) == current["_OLD_ENTITY_TYPES"] + current["_TABLES"] + (
        "business_document_accruals",
        "business_invoice_profiles",
        "business_invoice_reservations",
        "business_invoice_completions",
        "interledger_settings",
        "interledger_groups",
    )
    original_fks = [
        fk for fk in table.foreign_key_constraints if fk.name != "fk_document_revision_line_source"
    ]
    sql = str(
        CreateTable(table, include_foreign_key_constraints=original_fks).compile(dialect=dialect())
    )
    assert " ".join(sql.split()) in " ".join(current["_SCHEMA_SQL"].split())
    (source,) = table.foreign_key_constraints.difference(original_fks)
    assert [e.target_fullname for e in source.elements] == [
        "business_document_lines.id",
        "business_document_lines.document_id",
        "business_document_lines.ledger_id",
    ]
    assert source.deferrable and source.initially == "DEFERRED"


def test_source_registration_preserves_frozen_confirmation_and_draft_guard_bodies():
    versions = Path(__file__).resolve().parents[3] / "services/api/migrations/versions"
    initial = runpy.run_path(str(versions / "20261011_0027_business_confirmations.py"))
    activation = runpy.run_path(str(versions / "20261011_0028_business_accruals.py"))
    current = runpy.run_path(str(versions / "20261110_0077_document_revision_line_identities.py"))
    assert current["down_revision"] == "20261110_0076"
    for old, field in (
        (initial["_COMPLETE_SQL"], "_OLD_COMPLETE"),
        (initial["_SNAPSHOT_SQL"], "_OLD_SNAPSHOT"),
        (activation["_LINE"], "_OLD_LINE"),
    ):
        assert (
            old.strip().replace("CREATE FUNCTION", "CREATE OR REPLACE FUNCTION")
            == current[field].strip()
        )
    assert current["_SNAPSHOT"].replace(current["_ORIGINAL"], "") == current["_OLD_SNAPSHOT"]
    assert (
        current["_COMPLETE"]
        .replace(current["_ORIGINAL"], "")
        .replace(
            "FROM public.business_document_lines l\n      WHERE",
            "FROM public.business_document_lines\n      WHERE",
        )
        == current["_OLD_COMPLETE"]
    )
    bounded = current["_LINE"].index("IF TG_OP IN ('INSERT','UPDATE') AND EXISTS (")
    prior = current["_OLD_LINE"].index("IF TG_OP IN ('INSERT','UPDATE') AND EXISTS (")
    assert current["_LINE"][bounded:] == current["_OLD_LINE"][prior:]


def test_accrual_links_have_the_frozen_scoped_journal_and_source_constraints():
    from coinpup_api.business.document_revision_models import BusinessDocumentAccrual

    versions = Path(__file__).resolve().parents[3] / "services/api/migrations/versions"
    old = runpy.run_path(str(versions / "20261110_0076_confirmed_document_revision_lines.py"))
    current = runpy.run_path(str(versions / "20261110_0078_document_accrual_members.py"))
    assert current["down_revision"] == "20261110_0077"
    assert current["_OLD_ENTITY_TYPES"] == old["_OLD_ENTITY_TYPES"] + old["_TABLES"]
    table = BusinessDocumentAccrual.__table__
    assert current["_TABLES"] == (table.name,)
    assert tuple(get_args(EntityType)) == current["_OLD_ENTITY_TYPES"] + current["_TABLES"] + (
        "business_invoice_profiles",
        "business_invoice_reservations",
        "business_invoice_completions",
        "interledger_settings",
        "interledger_groups",
    )
    sql = str(CreateTable(table).compile(dialect=dialect()))
    assert " ".join(sql.split()) in " ".join(current["_SCHEMA_SQL"].split())
