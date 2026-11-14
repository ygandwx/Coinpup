"""Fictional credits consume gross capacity without becoming settlement applications."""

import ast
import runpy
from datetime import date
from decimal import Decimal, localcontext
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from coinpup_api.business.settlement_balances import quantities
from coinpup_api.business.settlement_schemas import BusinessAllocationCreate
from coinpup_api.business.settlement_totals import document_totals
from coinpup_api.business.settlements import allocation_plan
from coinpup_api.ledger.assets import ASSETS
from coinpup_api.ledger.errors import MoneyError
from coinpup_api.ledger.service import LedgerError
from sqlalchemy.dialects.postgresql import dialect


def test_document_components_use_one_bounded_query_and_keep_original_meanings():
    ledger, line = uuid4(), uuid4()
    statements = []

    def execute(statement):
        statements.append(statement)
        return [(line, Decimal("60"), Decimal("17.23"), Decimal("2.77"))]

    session = SimpleNamespace(execute=execute)
    assert document_totals(session, ledger, []) == {} and not statements
    assert document_totals(session, ledger, [line]) == {
        line: (Decimal("60"), Decimal("17.23"), Decimal("2.77"))
    }
    assert len(statements) == 1
    compiled = statements[0].compile(dialect=dialect())
    sql = str(compiled)
    assert sql.count("UNION ALL") == 2
    assert sql.count(".ledger_id =") == 4  # Includes the same-ledger return join.
    assert list(compiled.params.values()).count(ledger) == 4
    assert sql.count(" IN ") == 2
    assert sql.count("public.coinpup_current_credit_lines(") == 1
    assert list(compiled.params.values()).count([line]) == 3
    assert str(line) not in sql


def test_credit_gross_is_separate_from_applied_and_never_uses_decimal_context():
    with localcontext() as context:
        context.prec = 1
        assert quantities(Decimal("110"), Decimal("60"), ASSETS["USD"], credited=Decimal("20")) == (
            "110.00",
            "60.00",
            "30.00",
        )
        assert quantities(
            Decimal("99999999999999999999.99"),
            Decimal("99999999999999999999.97"),
            ASSETS["USD"],
            credited=Decimal("0.01"),
        ) == ("99999999999999999999.99", "99999999999999999999.97", "0.01")
        assert quantities(Decimal("110"), Decimal("60"), ASSETS["USD"]) == (
            "110.00",
            "60.00",
            "50.00",
        )


@pytest.mark.parametrize(
    "applied,credit", [("-1", "0"), ("111", "0"), ("60", "-1"), ("60", "50.01")]
)
def test_inconsistent_credit_capacity_is_rejected_not_clamped(applied, credit):
    with pytest.raises(LedgerError) as error:
        quantities(Decimal("110"), Decimal(applied), ASSETS["USD"], credited=Decimal(credit))
    assert error.value.code == "settlement_integrity"


def test_credit_capacity_preserves_refund_sql_and_exact_empty_history_downgrade():
    root = Path(__file__).resolve().parents[3]
    versions = root / "services/api/migrations/versions"
    previous = runpy.run_path(str(versions / "20261015_0036_business_refund_validation.py"))
    path = versions / "20261021_0038_business_credit_capacity.py"
    migration = runpy.run_path(str(path))
    assert migration["_OLD_ALLOCATION"] == previous["_ALLOCATION"]
    assert migration["_ALLOCATION"].count("FROM public.business_credit_lines c") == 1
    # The refund-capacity fix, all original shape checks and error names remain intact.
    added = (
        "\n              + (SELECT coalesce(sum(c.total_amount),0) "
        "FROM public.business_credit_lines c"
        "\n                 WHERE c.source_line_id=item.document_line_id"
        "\n                   AND c.ledger_id=allocation.ledger_id)"
    )
    assert migration["_ALLOCATION"].replace(added, "") == previous["_ALLOCATION"]
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.ImportFrom):
            assert node.module == "alembic"
        assert not isinstance(node, ast.Import)
    scripts = ScriptDirectory.from_config(Config(str(root / "alembic.ini")))
    assert scripts.get_heads() == ["20261109_0049"]
    assert scripts.get_revision("20261021_0038").down_revision == "20261021_0037"


@pytest.mark.parametrize("amount,accepted", [("30", True), ("30.01", False)])
def test_allocation_plan_reserves_gross_credit_without_relabeling_applications(
    monkeypatch, amount, accepted
):
    ledger, party, document_id, line_id, payment_id = (uuid4() for _ in range(5))
    document = SimpleNamespace(
        id=document_id,
        asset_id="USD",
        party_id=party,
        document_kind="bill",
        issue_date=date(2026, 10, 1),
    )
    line = SimpleNamespace(
        id=line_id,
        total_amount=Decimal("110"),
        net_amount=Decimal("100"),
        tax_amount=Decimal("10"),
        project_id=None,
    )
    payment = SimpleNamespace(
        id=payment_id,
        asset_id="USD",
        party_id=party,
        direction="payout",
        transaction_date=date(2026, 10, 1),
        amount=Decimal("100"),
    )
    request = BusinessAllocationCreate(
        id=uuid4(),
        payment_id=payment_id,
        allocation_date="2026-10-21",
        lines=[
            dict(
                document_id=document_id,
                document_line_id=line_id,
                payment_amount=amount,
                document_amount=amount,
            )
        ],
    )
    monkeypatch.setattr("coinpup_api.business.settlements.payment_totals", lambda *a: {})
    monkeypatch.setattr(
        "coinpup_api.business.settlements.document_totals",
        lambda *a: {line_id: (Decimal("60"), Decimal("17.23"), Decimal("2.77"))},
    )
    args = (None, ledger, payment, request, {line_id: (document, line)}, {"USD": ASSETS["USD"]})
    if accepted:
        with localcontext() as context:
            context.prec = 1
            result = allocation_plan(*args, new_payment=False)
        assert result.lines[0].document_amount.to_string() == "30.00"
    else:
        with pytest.raises(MoneyError) as failure:
            allocation_plan(*args, new_payment=False)
        assert failure.value.code == "allocation_exceeds_document"
