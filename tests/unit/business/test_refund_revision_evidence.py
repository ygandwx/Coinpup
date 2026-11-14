"""The return table adds exactly one sync type and keeps a frozen composite-key definition."""

import runpy
from pathlib import Path
from typing import get_args

from coinpup_api.business.refund_revision_return_models import BusinessRefundRevisionReturn
from coinpup_api.ledger import models as ledger_models  # noqa: F401
from coinpup_api.sync.schemas import EntityType
from sqlalchemy.dialects.postgresql import dialect
from sqlalchemy.schema import CreateIndex, CreateTable


def test_evidence_extends_the_catalog_and_matches_frozen_postgres_ddl():
    versions = Path(__file__).resolve().parents[3] / "services/api/migrations/versions"
    schema = runpy.run_path(str(versions / "20261109_0050_refund_revision_evidence.py"))
    prior = runpy.run_path(str(versions / "20261109_0049_refund_revision_headers.py"))
    assert schema["down_revision"] == prior["revision"]
    assert schema["_OLD_ENTITY_TYPES"] == prior["_OLD_ENTITY_TYPES"] + prior["_TABLES"]
    assert schema["_TABLES"] == ("business_refund_revision_returns",)
    assert set(get_args(EntityType)) == set(schema["_OLD_ENTITY_TYPES"] + schema["_TABLES"])
    table = BusinessRefundRevisionReturn.__table__
    assert len(table.name) <= 32
    for stmt in (CreateTable(table), *(CreateIndex(i) for i in table.indexes)):
        assert " ".join(str(stmt.compile(dialect=dialect())).split()) in " ".join(
            schema["_SCHEMA_SQL"].split()
        )
