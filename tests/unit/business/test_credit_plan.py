"""Fictional manual credits preserve exact source quantities and classification."""

from dataclasses import FrozenInstanceError, replace
from decimal import localcontext
from uuid import uuid4

import pytest
from coinpup_api.business.credit_plan import CreditSource, plan_credit_line
from coinpup_api.ledger.assets import ASSETS, AssetDefinition
from coinpup_api.ledger.errors import MoneyError
from coinpup_api.ledger.money import Amount


def source(kind="bill", asset=ASSETS["USD"], net="100", tax="10"):
    net, tax = Amount.parse(net, asset), Amount.parse(tax, asset)
    return CreditSource(
        uuid4(),
        uuid4(),
        uuid4(),
        kind,
        uuid4(),
        uuid4(),
        net,
        tax,
        net + tax,
        Amount(asset, 0),
        Amount(asset, 0),
    )


@pytest.mark.parametrize(
    "kind,sign,role,key",
    [
        ("bill", -1, "expense", "payable.supplier"),
        ("invoice", 1, "income", "receivable.customer"),
    ],
)
def test_manual_credit_reduces_original_category_without_cash(kind, sign, role, key):
    original = source(kind)
    with localcontext() as context:
        context.prec = 1
        plan = plan_credit_line(original.ledger_id, original, "17.23", "2.77")
    assert plan.net.to_string() == "17.23" and plan.tax.to_string() == "2.77"
    assert plan.total.to_string() == "20.00"
    assert plan.category_amount.minor_units == sign * 2000
    assert plan.category_amount + plan.control_amount == Amount(ASSETS["USD"], 0)
    assert (plan.category_role, plan.control_key) == (role, key)
    assert (plan.document_id, plan.line_id, plan.category_id, plan.project_id) == (
        original.document_id,
        original.line_id,
        original.category_id,
        original.project_id,
    )
    assert (plan.remaining_net.to_string(), plan.remaining_tax.to_string()) == ("82.77", "7.23")
    assert original.credited_net.minor_units == original.credited_tax.minor_units == 0
    with pytest.raises(FrozenInstanceError):
        plan.category_id = uuid4()


def test_partial_then_final_credit_consumes_each_capacity_exactly():
    original = source()
    first = plan_credit_line(original.ledger_id, original, "82.77", "7.23")
    later = replace(original, credited_net=first.net, credited_tax=first.tax, project_id=None)
    final = plan_credit_line(later.ledger_id, later, "17.23", "2.77")
    assert final.remaining_net.minor_units == final.remaining_tax.minor_units == 0
    assert final.project_id is None
    full = replace(later, credited_net=original.net, credited_tax=original.tax)
    with pytest.raises(MoneyError, match="Credit exceeds"):
        plan_credit_line(full.ledger_id, full, "0.01", "0")


@pytest.mark.parametrize("net,tax", [("100", "0"), ("0", "10")])
def test_one_zero_component_is_explicitly_allowed(net, tax):
    original = source()
    plan = plan_credit_line(original.ledger_id, original, net, tax)
    assert plan.total.minor_units > 0


@pytest.mark.parametrize(
    "net,tax,code",
    [
        ("0", "0", "credit_amount"),
        ("-1", "2", "credit_amount"),
        ("2", "-1", "credit_amount"),
        ("100.01", "0", "credit_exceeds_source"),
        ("0", "10.01", "credit_exceeds_source"),
        ("1.000", "0", "amount_precision"),
        ("1", "0.000", "amount_precision"),
        (1.0, "0", "amount_type"),
        ("1e0", "0", "amount_format"),
        ("1", True, "amount_type"),
    ],
)
def test_rejects_invalid_or_individually_excessive_manual_components(net, tax, code):
    original = source()
    with pytest.raises(MoneyError) as failure:
        plan_credit_line(original.ledger_id, original, net, tax)
    assert failure.value.code == code


@pytest.mark.parametrize("asset", [ASSETS["USD"], ASSETS["BTC"], ASSETS["ETH"]])
def test_minimum_and_maximum_quantities_are_independent_of_decimal_context(asset):
    maximum = Amount(asset, 10 ** (20 + asset.scale) - 1)
    minimum = Amount(asset, 1)
    original = source(asset=asset, net=(maximum - minimum).to_string(), tax=minimum.to_string())
    with localcontext() as context:
        context.prec = 1
        plan = plan_credit_line(
            original.ledger_id, original, original.net.to_string(), minimum.to_string()
        )
    assert plan.total == maximum
    assert plan.remaining_net.minor_units == plan.remaining_tax.minor_units == 0


@pytest.mark.parametrize(
    "field,value",
    [
        ("document_id", "fictional"),
        ("line_id", None),
        ("category_id", "fictional"),
        ("project_id", "fictional"),
        ("document_kind", "payment"),
        ("net", Amount(ASSETS["USD"], -1)),
        ("tax", Amount(ASSETS["USD"], -1)),
        ("total", Amount(ASSETS["USD"], 0)),
        ("total", Amount(ASSETS["USD"], 10999)),
        ("credited_net", Amount(ASSETS["USD"], -1)),
        ("credited_tax", Amount(ASSETS["USD"], -1)),
        ("credited_net", Amount(ASSETS["USD"], 10001)),
        ("credited_tax", Amount(ASSETS["USD"], 1001)),
        ("net", "100"),
        ("total", None),
        ("tax", Amount(ASSETS["EUR"], 1000)),
    ],
)
def test_inconsistent_source_is_rejected_without_coercion(field, value):
    original = replace(source(), **{field: value})
    with pytest.raises(MoneyError) as failure:
        plan_credit_line(original.ledger_id, original, "1", "0")
    assert failure.value.code == "credit_integrity"


def test_cross_ledger_and_non_source_objects_are_rejected():
    original = source()
    for ledger, candidate in [
        (uuid4(), original),
        (str(original.ledger_id), original),
        (original.ledger_id, None),
    ]:
        with pytest.raises(MoneyError) as failure:
            plan_credit_line(ledger, candidate, "1", "0")
        assert failure.value.code == "credit_integrity"


@pytest.mark.parametrize(
    "change", [{"network": "fictionalnet"}, {"scale": 8}, {"token_reference": "fictional-two"}]
)
def test_full_token_identity_must_match(change):
    token = AssetDefinition("USDC", "token", 6, "ethereum", "fictional-one")
    original = source(asset=token)
    other = replace(token, **change)
    original = replace(original, credited_tax=Amount(other, 0))
    with pytest.raises(MoneyError) as failure:
        plan_credit_line(original.ledger_id, original, "1", "0")
    assert failure.value.code == "credit_integrity"
