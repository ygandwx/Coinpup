"""Frozen additive refund schema matches runtime metadata and one migration chain."""

import ast
import runpy
from pathlib import Path
from typing import get_args

from alembic.config import Config
from alembic.script import ScriptDirectory
from coinpup_api.business.refund_models import BusinessRefund, BusinessRefundReturn
from coinpup_api.ledger import models as ledger_models  # noqa: F401
from coinpup_api.sync.schemas import EntityType
from sqlalchemy.dialects.postgresql import dialect
from sqlalchemy.schema import CreateIndex, CreateTable


def test_refund_storage_is_frozen_and_matches_all_declared_tables_and_indexes():
    root = Path(__file__).resolve().parents[3]
    path = root / "services/api/migrations/versions/20261015_0035_business_refund_storage.py"
    migration = runpy.run_path(str(path))
    previous = runpy.run_path(str(path.with_name("20261014_0033_allocation_return_storage.py")))
    assert migration["_OLD_ENTITY_TYPES"] == previous["_OLD_ENTITY_TYPES"] + previous["_TABLES"]
    assert len(set(migration["_OLD_ENTITY_TYPES"] + migration["_TABLES"])) == 34
    assert set(migration["_OLD_ENTITY_TYPES"] + migration["_TABLES"]) <= set(get_args(EntityType))
    frozen = " ".join(migration["_SCHEMA_SQL"].split())
    for model in (BusinessRefund, BusinessRefundReturn):
        statements = [CreateTable(model.__table__)]
        statements.extend(CreateIndex(index) for index in model.__table__.indexes)
        for statement in statements:
            assert " ".join(str(statement.compile(dialect=dialect())).split()) in frozen
        assert model.__tablename__ in migration["_TABLES"]
        assert len(model.__tablename__) <= 32
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.ImportFrom):
            assert node.module == "alembic"
        elif isinstance(node, ast.Import):
            raise AssertionError("Migration imports runtime code")
    scripts = ScriptDirectory.from_config(Config(str(root / "alembic.ini")))
    assert len(scripts.get_heads()) == 1
    assert scripts.get_revision("20261015_0035").down_revision == "20261014_0034"
