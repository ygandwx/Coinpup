"""Frozen additive storage registers pending evidence without opening an incomplete write path."""

import ast
from pathlib import Path
from typing import get_args

from coinpup_api.business.invoice_reservation_models import InvoiceReservation
from coinpup_api.files.models import StoredFile
from coinpup_api.sync.schemas import EntityType
from sqlalchemy.dialects.postgresql import dialect
from sqlalchemy.schema import CreateTable

from tests.integration.conftest import invoice_profile_migration, invoice_reservation_migration


def test_frozen_reservation_schema_matches_model_and_current_notification_types():
    migration = invoice_reservation_migration()
    previous = invoice_profile_migration()
    assert migration["revision"] == "20261113_0098"
    assert migration["down_revision"] == "20261113_0097"
    assert migration["_OLD_ENTITY_TYPES"] == previous["_OLD_ENTITY_TYPES"] + previous["_TABLES"]
    assert tuple(get_args(EntityType)) == migration["_OLD_ENTITY_TYPES"] + migration["_TABLES"] + (
        "business_invoice_completions",
        "interledger_settings",
        "interledger_groups",
        "manual_prices",
        "reminder_email_deliveries",
        "reminder_email_evidence",
    )
    assert InvoiceReservation.__table__.c.logo_file_id.references(StoredFile.__table__.c.id)
    statement = CreateTable(InvoiceReservation.__table__).compile(dialect=dialect())
    assert " ".join(str(statement).split()) in " ".join(migration["_SCHEMA_SQL"].split())
    path = Path("services/api/migrations/versions/20261113_0098_invoice_reservations.py")
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.ImportFrom):
            assert node.module == "alembic"
        assert not isinstance(node, ast.Import)
