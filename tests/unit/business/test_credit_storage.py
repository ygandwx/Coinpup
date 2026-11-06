"""The additive credit schema is frozen and mirrors the complete current model catalogue."""

import ast
import runpy
from pathlib import Path
from typing import get_args

from alembic.config import Config
from alembic.script import ScriptDirectory
from coinpup_api.business.credit_models import BusinessCredit, BusinessCreditLine
from coinpup_api.ledger import models as ledger_models  # noqa: F401
from coinpup_api.sync.schemas import EntityType
from sqlalchemy.dialects.postgresql import dialect
from sqlalchemy.schema import CreateIndex, CreateTable


def test_credit_models_match_frozen_sql_and_current_change_types():
    root = Path(__file__).resolve().parents[3]
    path = root / "services/api/migrations/versions/20261021_0037_business_credit_storage.py"
    migration = runpy.run_path(str(path))
    previous = runpy.run_path(str(path.with_name("20261015_0035_business_refund_storage.py")))
    assert migration["_OLD_ENTITY_TYPES"] == previous["_OLD_ENTITY_TYPES"] + previous["_TABLES"]
    assert len(set(migration["_OLD_ENTITY_TYPES"] + migration["_TABLES"])) == 36
    assert set(get_args(EntityType)) == (
        set(migration["_OLD_ENTITY_TYPES"] + migration["_TABLES"]) | {"business_credit_revisions"}
    )
    frozen = " ".join(migration["_SCHEMA_SQL"].split())
    for model in (BusinessCredit, BusinessCreditLine):
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
    assert scripts.get_heads() == ["20261106_0040"]
    assert scripts.get_revision("20261021_0037").down_revision == "20261015_0036"
