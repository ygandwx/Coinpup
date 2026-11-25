"""The frozen return schema preserves the previous notification catalog and scoped row identity."""

import runpy
from pathlib import Path
from typing import get_args

from coinpup_api.business.allocation_return_revision_models import BusinessAllocationReturnRevision
from coinpup_api.ledger import models as ledger_models  # noqa: F401
from coinpup_api.sync.schemas import EntityType
from sqlalchemy.dialects.postgresql import dialect
from sqlalchemy.schema import CreateTable


def test_return_header_matches_frozen_ddl_and_fits_existing_change_log_type():
    versions = Path(__file__).resolve().parents[3] / "services/api/migrations/versions"
    current = runpy.run_path(str(versions / "20261109_0059_allocation_return_revision_headers.py"))
    prior = runpy.run_path(str(versions / "20261109_0050_refund_revision_evidence.py"))
    table = BusinessAllocationReturnRevision.__table__
    assert current["down_revision"] == "20261109_0058"
    assert current["_OLD_ENTITY_TYPES"] == prior["_OLD_ENTITY_TYPES"] + prior["_TABLES"]
    assert current["_TABLES"] == (table.name,) and len(table.name) <= 32
    assert set(get_args(EntityType)) == set(current["_OLD_ENTITY_TYPES"] + current["_TABLES"]) | {
        "business_return_revision_members",
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
    assert " ".join(str(CreateTable(table).compile(dialect=dialect())).split()) in " ".join(
        current["_SCHEMA_SQL"].split()
    )
