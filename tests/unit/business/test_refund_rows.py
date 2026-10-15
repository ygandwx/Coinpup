"""Refund postings preserve each asset and never recognize principal as expense."""

from dataclasses import replace
from decimal import localcontext
from types import SimpleNamespace
from uuid import uuid4

import pytest
from coinpup_api.business.refund_rows import refund_rows
from coinpup_api.business.refund_schemas import BusinessRefundCreate
from coinpup_api.ledger.assets import ASSETS
from coinpup_api.ledger.commands.fees import FeeCommands
from coinpup_api.ledger.money import Amount
from coinpup_api.ledger.service import LedgerError

from tests.unit.business.test_refund_plan import fixture, run


class FeeBoundary(FeeCommands):
    def __init__(self):
        self.checked_accounts = []

    def _account(self, session, ledger_id, account_id, asset_id):
        self.checked_accounts.append((ledger_id, account_id, asset_id))


def prepared(args, *, category_kind="expense"):
    ledger, payment, _refunded, payload, _returns = args
    boundary = FeeBoundary()
    session = SimpleNamespace(scalar=lambda _: SimpleNamespace(kind=category_kind, archived=False))
    plan = run(args)
    control = uuid4()
    with localcontext() as context:
        context.prec = 1
        result = refund_rows(
            boundary,
            session,
            ledger,
            payment.party_id,
            payload,
            plan,
            {plan.control_key: SimpleNamespace(id=control)},
            ASSETS,
        )
    return result, boundary, control


@pytest.mark.parametrize("direction,sign", [("receipt", -1), ("payout", 1)])
@pytest.mark.parametrize("asset,actual", [("USD", "20"), ("EUR", "19.37"), ("BTC", "0.00000001")])
def test_refund_principal_is_balanced_per_asset_and_keeps_only_party_dimension(
    direction, sign, asset, actual
):
    args = fixture(direction, asset, "20", actual)
    result, boundary, control = prepared(args)
    assert len(result.lines) == (2 if asset == "USD" else 4)
    assert result.description == args[3].reason and not boundary.checked_accounts
    principal, advance = result.lines[:2]
    assert principal.account_id == args[3].account_id
    assert principal.amount.minor_units == sign * Amount.parse(actual, ASSETS[asset]).minor_units
    assert advance.account_id == control and advance.amount.minor_units == -sign * 2000
    totals = {}
    for line, stored in zip(result.lines, result.parameters, strict=True):
        assert line.journal_id == result.journal_id and line.ledger_id == args[0]
        assert line.party_id == args[1].party_id and line.component_no == 0
        assert all(
            getattr(line, key) is None
            for key in (
                "category_id",
                "project_id",
                "document_id",
                "document_line_id",
                "counterparty_entity_id",
                "dimension_owner_id",
            )
        )
        assert line.role in {"account", "exchange"}
        assert Amount.from_decimal(stored["amount"], ASSETS[line.asset_id]) == line.amount
        totals[line.asset_id] = totals.get(line.asset_id, 0) + line.amount.minor_units
    assert set(totals.values()) == {0}


def test_only_explicit_fees_create_expense_with_independent_components_and_no_business_dimensions():
    args = list(fixture("payout", "EUR", "22", "19.37"))
    fees = [
        dict(account_id=uuid4(), asset_id=asset, amount=amount, category_id=uuid4())
        for asset, amount in (("USD", "0.01"), ("BTC", "0.00000001"))
    ]
    args[3] = BusinessRefundCreate.model_validate(args[3].model_dump() | {"fees": fees})
    result, boundary, _ = prepared(args)
    assert len(result.lines) == 8 and len(boundary.checked_accounts) == 2
    assert [line.line_no for line in result.lines] == list(range(1, 9))
    for index, fee in enumerate(fees, 1):
        pair = [line for line in result.lines if line.component_no == index]
        bank, expense = pair
        assert bank.role == "account" and expense.role == "expense"
        assert bank.account_id == fee["account_id"] and expense.category_id == fee["category_id"]
        assert bank.amount + expense.amount == Amount(ASSETS[fee["asset_id"]], 0)
        assert expense.amount.to_string() == fee["amount"]
        assert all(line.party_id is None and line.document_id is None for line in pair)
    assert [line for line in result.lines[:4] if line.role in {"income", "expense"}] == []
    with pytest.raises(LedgerError) as caught:
        prepared(args, category_kind="income")
    assert caught.value.code == "category_kind_mismatch"


def test_maximum_refund_stays_exact_with_low_decimal_context():
    maximum = "99999999999999999999.99"
    args = list(fixture(principal=maximum, actual=maximum))
    args[1] = replace(
        args[1], amount=Amount.parse(maximum, ASSETS["USD"]), applied=Amount(ASSETS["USD"], 0)
    )
    args[2] = Amount(ASSETS["USD"], 0)
    result, _, _ = prepared(args)
    assert result.lines[0].amount.to_string() == maximum
    assert str(result.parameters[0]["amount"]) == maximum
