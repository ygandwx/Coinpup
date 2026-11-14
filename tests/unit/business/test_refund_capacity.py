"""Refunds reduce available original principal without relabeling them as applications."""

from decimal import Decimal, localcontext
from types import SimpleNamespace
from uuid import uuid4

import pytest
from coinpup_api.business.settlement_balances import quantities
from coinpup_api.business.settlement_totals import payment_totals
from coinpup_api.ledger.assets import ASSETS
from coinpup_api.ledger.service import LedgerError
from sqlalchemy.dialects.postgresql import dialect


def test_payment_totals_are_one_scoped_query_and_keep_both_quantities_separate():
    ledger, payment = uuid4(), uuid4()
    statements = []

    def execute(statement):
        statements.append(statement)
        return [(payment, Decimal("80"), Decimal("10"))]

    session = SimpleNamespace(execute=execute)
    assert payment_totals(session, ledger, []) == {} and not statements
    assert payment_totals(session, ledger, iter([])) == {} and not statements
    assert payment_totals(session, ledger, iter([payment])) == {
        payment: (Decimal("80"), Decimal("10"))
    }
    assert len(statements) == 1
    compiled = statements[0].compile(dialect=dialect())
    assert str(compiled).count("UNION ALL") == 2
    assert str(compiled).count(".ledger_id =") == 2
    assert str(compiled).count(".payment_id IN") == 2
    assert list(compiled.params.values()).count(ledger) == 3
    assert str(compiled).count("public.coinpup_current_refund_principals(") == 1
    assert "business_refunds" not in str(compiled)
    assert list(compiled.params.values()).count([payment]) == 3
    assert str(payment) not in str(compiled)


def test_refund_capacity_retains_original_and_applied_amount_with_exact_remaining():
    with localcontext() as context:
        context.prec = 1
        assert quantities(Decimal("100"), Decimal("80"), ASSETS["USD"], refunded=Decimal("10")) == (
            "100.00",
            "80.00",
            "10.00",
        )
        assert (
            quantities(
                Decimal("99999999999999999999.99"),
                Decimal("99999999999999999999.97"),
                ASSETS["USD"],
                refunded=Decimal("0.01"),
            )[2]
            == "0.01"
        )


@pytest.mark.parametrize("used,refunded", [("-1", "0"), ("101", "0"), ("80", "-1"), ("80", "21")])
def test_inconsistent_refund_or_application_totals_are_not_silently_clamped(used, refunded):
    with pytest.raises(LedgerError) as error:
        quantities(Decimal("100"), Decimal(used), ASSETS["USD"], refunded=Decimal(refunded))
    assert error.value.code == "settlement_integrity"
