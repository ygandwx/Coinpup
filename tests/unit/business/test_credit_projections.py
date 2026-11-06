"""The projection contract is typed and cannot replace or open existing financial rules."""

import ast
import runpy
from pathlib import Path
from uuid import uuid4

from coinpup_api.business.credit_projections import current_credit_lines, posting_facts
from sqlalchemy import Numeric, select
from sqlalchemy.dialects.postgresql import dialect


def test_projection_migration_is_additive_frozen_and_read_only():
    path = Path(__file__).resolve().parents[3] / (
        "services/api/migrations/versions/20261106_0042_credit_read_projections.py"
    )
    source = path.read_text(encoding="utf-8")
    migration = runpy.run_path(str(path))
    assert migration["down_revision"] == "20261106_0041"
    assert source.count("CREATE FUNCTION ") == 2
    assert "CREATE OR REPLACE" not in source
    assert "CREATE TRIGGER" not in source
    for sql in (migration["_FACTS"], migration["_CURRENT"]):
        assert "STABLE SECURITY INVOKER SET search_path=pg_catalog" in sql
        assert not any(word in sql for word in ("UPDATE ", "DELETE ", "INSERT ", "FOR UPDATE"))
    assert "public.coinpup_credit_posting_facts(" in migration["_CURRENT"]
    assert "financial_operations" not in source  # Current pointers never determine old facts.
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.ImportFrom):
            assert node.module == "alembic"
        assert not isinstance(node, ast.Import)


def test_selectables_keep_uuid_arrays_and_exact_decimal_columns():
    ledger, source = uuid4(), uuid4()
    for relation in (
        current_credit_lines(ledger, [source]),
        posting_facts(ledger, journal_ids=[source]),
    ):
        sql = str(select(relation).compile(dialect=dialect()))
        assert "public.coinpup_" in sql and "UUID[]" in sql
        assert "LIMIT" not in sql
        assert len(relation.c) == 18
        for name in ("net_amount", "tax_amount", "total_amount"):
            assert isinstance(relation.c[name].type, Numeric)
            assert (relation.c[name].type.precision, relation.c[name].type.scale) == (38, 18)
