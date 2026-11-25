"""Exact frozen member DDL, source scope and change-feed identity."""

import runpy
from pathlib import Path
from typing import get_args

from coinpup_api.business.allocation_return_revision_member_models import (
    BusinessReturnRevisionMember,
)
from coinpup_api.ledger import models as ledger_models  # noqa: F401
from coinpup_api.sync.schemas import EntityType
from sqlalchemy.dialects.postgresql import dialect
from sqlalchemy.schema import CreateIndex, CreateTable


def test_member_freezes_the_real_table_and_extends_only_the_accepted_header_catalog():
    versions = Path(__file__).resolve().parents[3] / "services/api/migrations/versions"
    migration = runpy.run_path(
        str(versions / "20261109_0061_allocation_return_revision_members.py")
    )
    header = runpy.run_path(str(versions / "20261109_0059_allocation_return_revision_headers.py"))
    assert migration["down_revision"] == "20261109_0060"
    assert migration["_OLD_ENTITY_TYPES"] == header["_OLD_ENTITY_TYPES"] + header["_TABLES"]
    t = BusinessReturnRevisionMember.__table__
    assert migration["_TABLES"] == (t.name,) and len(t.name) <= 32
    assert set(get_args(EntityType)) == set(
        migration["_OLD_ENTITY_TYPES"] + migration["_TABLES"]
    ) | {
        "business_document_revisions",
        "business_document_revision_lines",
        "business_document_accruals",
        "business_invoice_profiles",
        "business_invoice_reservations",
        "business_invoice_completions",
        "interledger_settings",
        "interledger_groups",
        "manual_prices",
        "reminder_email_deliveries",
        "reminder_email_evidence",
        "quote_observations",
        "reminder_inbox_deliveries",
    }
    for stmt in (CreateTable(t), *(CreateIndex(i) for i in t.indexes)):
        assert " ".join(str(stmt.compile(dialect=dialect())).split()) in " ".join(
            migration["_SCHEMA_SQL"].split()
        )
