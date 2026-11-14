"""Refund validation is additive and restores the exact frozen prior dispatch/capacity."""

import ast
import runpy
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory
from coinpup_api.ledger.models import FinancialOperation


def test_refund_sql_is_frozen_additive_and_keeps_one_migration_head():
    root = Path(__file__).resolve().parents[3]
    versions = root / "services/api/migrations/versions"
    path = versions / "20261015_0036_business_refund_validation.py"
    previous = runpy.run_path(str(versions / "20261014_0034_allocation_return_validation.py"))
    migration = runpy.run_path(str(path))
    assert migration["_OLD_ALLOCATION"] == previous["_ALLOCATION"]
    assert migration["_OLD_DISPATCH"] == previous["_DISPATCH"]
    assert migration["_OLD_KINDS"] == previous["_KINDS"]
    assert migration["_ALLOCATION"] != migration["_OLD_ALLOCATION"]
    assert migration["_DISPATCH"].count("WHEN 'refund'") == 1
    assert "REFUND_FEE_VALIDATION" not in migration["_REFUND"]
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.ImportFrom):
            assert node.module == "alembic"
        assert not isinstance(node, ast.Import)
    constraint = next(
        c
        for c in FinancialOperation.__table__.constraints
        if c.name == "ck_financial_operations_kind"
    )
    current = runpy.run_path(str(versions / "20261021_0039_business_credit_validation.py"))
    assert current["_OLD_KINDS"] == migration["_KINDS"]
    assert str(constraint.sqltext) == current["_KINDS"]
    scripts = ScriptDirectory.from_config(Config(str(root / "alembic.ini")))
    assert scripts.get_heads() == ["20261109_0057"]
    assert scripts.get_revision("20261015_0036").down_revision == "20261015_0035"


def test_current_principal_consumers_replace_only_the_refund_capacity_terms():
    versions = Path(__file__).resolve().parents[3] / "services/api/migrations/versions"
    allocation = runpy.run_path(str(versions / "20261106_0046_credit_current_capacity.py"))
    refund = runpy.run_path(str(versions / "20261015_0036_business_refund_validation.py"))
    capacity = runpy.run_path(str(versions / "20261109_0052_refund_current_capacity.py"))
    assert capacity["_OLD_ALLOCATION"].strip() == allocation["_ALLOCATION"].strip()
    assert (
        capacity["_OLD_REFUND"].strip()
        == refund["_REFUND"].split("CREATE FUNCTION coinpup_validate_shape_refund(")[0].strip()
    )
    for kind, alias, scope, separator in (
        ("ALLOCATION", "f", "allocation", " "),
        ("REFUND", "r", "entry", "\n           "),
    ):
        old = f"public.business_refunds {alias}{separator}WHERE {alias}.payment_id=payment.id AND "
        old += f"{alias}.ledger_id={scope}.ledger_id"
        current = f"public.coinpup_current_refund_principals({scope}.ledger_id, ARRAY[payment.id])"
        previous = capacity[f"_OLD_{kind}"]
        assert previous.count(old) == 1
        assert capacity[f"_{kind}"] == previous.replace(old, f"{current} {alias}")
