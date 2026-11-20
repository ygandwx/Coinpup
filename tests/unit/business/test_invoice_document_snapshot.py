"""Exact, immutable renderer sources include all 200 rows and never reprice saved facts."""

from dataclasses import FrozenInstanceError, replace
from decimal import localcontext
from uuid import uuid4

import pytest
from coinpup_api.business.document_current import DocumentCurrent
from coinpup_api.business.invoice_document_snapshot import freeze_invoice_document
from coinpup_api.business.invoice_issuance_schemas import InvoiceIssuanceCreate
from coinpup_api.ledger.assets import ASSETS, stablecoin_asset
from coinpup_api.ledger.errors import MoneyError

from tests.unit.business.test_document_accrual_revision_plan import current


def source(values=(7, 0, 3), kind="invoice"):
    document, postings = current(values, kind)
    facts = DocumentCurrent(
        ledger_id=document.ledger_id,
        document_id=document.id,
        confirmation_id=document.confirmation_id,
        document_kind=document.document_kind,
        version=document.version,
        status="active",
        revision_id=uuid4(),
        document=document,
        postings=[
            dict(
                source_line_id=p.source_line_id,
                operation_id=p.operation_id,
                operation_version=p.version,
                journal_id=p.journal_id,
            )
            for p in postings
        ],
    )
    command = InvoiceIssuanceCreate(
        id=uuid4(),
        document_id=document.id,
        expected_document_version=document.version,
        configuration_id=uuid4(),
        expected_configuration_version=1,
        previous_issuance_id=None,
    )
    return facts, command


def freeze(facts, command, asset=ASSETS["USD"], **overrides):
    return freeze_invoice_document(
        facts, asset, ledger_id=overrides.get("ledger_id", facts.ledger_id), command=command
    )


@pytest.mark.parametrize("count", [1, 200])
def test_all_exact_sources_and_zero_rows_survive_immutable_snapshot(count):
    facts, command = source([i % 3 if i else 1 for i in range(count)])
    before = facts.model_dump(mode="json")
    with localcontext() as context:
        context.prec = 2
        frozen = freeze(facts, command)
    decoded = frozen.decode()
    assert decoded["current"] == before
    assert len(decoded["current"]["document"]["lines"]) == count
    assert "Fictional 经营项目".encode() in frozen.content
    assert decoded["asset"] == dict(
        code="USD", kind="fiat", scale=2, network=None, token_reference=None
    )
    assert "enabled" not in decoded["asset"]
    assert freeze(facts, command, replace(ASSETS["USD"], enabled=False)) == frozen
    assert len(frozen.sha256) == 64
    decoded["current"]["document"]["lines"].clear()
    facts.document.lines[0].description = "Changed fictional label"
    assert frozen.decode()["current"] == before
    assert freeze(facts, command).sha256 != frozen.sha256
    with pytest.raises(FrozenInstanceError):
        frozen.content = b"changed"


def test_equivalent_object_key_order_is_deterministic_but_source_string_spelling_is_retained():
    facts, command = source([1])
    facts.document.party_snapshot = {"name": "Fictional customer", "address": "Fictional address"}
    frozen = freeze(facts, command)
    facts.document.party_snapshot = dict(reversed(list(facts.document.party_snapshot.items())))
    assert freeze(facts, command) == frozen
    facts.document.lines[0].unit_price = "1.00"
    changed = freeze(facts, command)
    assert changed.sha256 != frozen.sha256
    assert changed.decode()["current"]["document"]["lines"][0]["unit_price"] == "1.00"


@pytest.mark.parametrize(
    "quantity,price,discount,rate,net,tax,total",
    [
        ("3", "19.99", "9.97", "8.25", "50.00", "4.13", "54.13"),
        (
            "1",
            "99999999999999999999.99",
            "0",
            "0",
            "99999999999999999999.99",
            "0.00",
            "99999999999999999999.99",
        ),
    ],
)
def test_confirmed_discount_tax_and_maximum_amount_are_preserved_exactly(
    quantity, price, discount, rate, net, tax, total
):
    facts, command = source([1])
    row = facts.document.lines[0]
    row.quantity, row.unit_price = quantity, price
    row.discount_amount, row.tax_rate_percent = discount, rate
    for target in (facts.document, row):
        target.net_amount, target.tax_amount, target.total_amount = net, tax, total
    before = facts.model_dump(mode="json")
    with localcontext() as context:
        context.prec = 2
        frozen = freeze(facts, command)
    assert frozen.decode()["current"] == before
    assert facts.model_dump(mode="json") == before


@pytest.mark.parametrize("scale", [0, 6, 18])
def test_minimum_unit_and_complete_token_identity_stay_exact_under_small_decimal_context(scale):
    facts, command = source([1])
    unit = stablecoin_asset(
        code="USDT", network="fictional", token_reference="CaseSensitive", scale=scale
    )
    amount = "1" if scale == 0 else "0." + "0" * (scale - 1) + "1"
    facts.document.asset_id = unit.asset_id
    row = facts.document.lines[0]
    row.asset_id = unit.asset_id
    row.unit_price = amount
    row.net_amount = row.total_amount = amount
    facts.document.net_amount = facts.document.total_amount = amount
    with localcontext() as context:
        context.prec = 2
        frozen = freeze(facts, command, unit)
    decoded = frozen.decode()
    assert decoded["asset"]["scale"] == scale
    assert decoded["asset"]["network"] == "fictional"
    assert decoded["asset"]["token_reference"] == "CaseSensitive"
    assert decoded["current"]["document"]["total_amount"] == amount


@pytest.mark.parametrize(
    "defect",
    [
        "line_count",
        "omitted_zero",
        "duplicate",
        "reversed",
        "ledger",
        "document",
        "asset",
        "category",
        "archived",
        "negative_position",
        "journal",
    ],
)
def test_incomplete_or_cross_scope_line_facts_are_never_frozen(defect):
    facts, command = source([1, 3, 0])
    d = facts.document
    if defect == "line_count":
        d.line_count = 2
    elif defect == "omitted_zero":
        d.lines.pop()
    elif defect == "duplicate":
        d.lines[1].id = d.lines[0].id
    elif defect == "reversed":
        d.lines.reverse()
    elif defect in {"ledger", "document"}:
        setattr(d.lines[0], defect + "_id", uuid4())
    elif defect == "asset":
        d.lines[0].asset_id = "EUR"
    elif defect == "category":
        d.lines[0].category_kind = "expense"
    elif defect == "archived":
        d.lines[0].archived = True
    elif defect == "negative_position":
        d.lines[0].line_no = -1
    else:
        facts.postings[1] = facts.postings[1].model_copy(
            update={"journal_id": facts.postings[0].journal_id}
        )
    with pytest.raises(MoneyError):
        freeze(facts, command)


@pytest.mark.parametrize("field", ["net_amount", "tax_amount", "total_amount"])
@pytest.mark.parametrize("header", [False, True])
def test_inconsistent_saved_prices_are_rejected_rather_than_replaced_by_recalculation(
    field, header
):
    facts, command = source([1])
    setattr(facts.document if header else facts.document.lines[0], field, "0.01")
    before = facts.model_dump(mode="json")
    with pytest.raises(MoneyError):
        freeze(facts, command)
    assert facts.model_dump(mode="json") == before


def test_cancelled_bill_archived_or_stale_version_never_becomes_an_invoice_source():
    facts, command = source()
    cancelled = facts.model_copy(update={"status": "cancelled", "document": None, "postings": []})
    bill, bill_command = source(kind="bill")
    for bad, intent in [
        (cancelled, command),
        (bill, bill_command),
        (
            facts,
            command.model_copy(
                update={"expected_document_version": command.expected_document_version + 1}
            ),
        ),
        (facts, command.model_copy(update={"document_id": uuid4()})),
    ]:
        with pytest.raises(MoneyError):
            freeze(bad, intent)
    with pytest.raises(MoneyError):
        freeze(facts, command, ledger_id=uuid4())
    facts.document.archived = True
    with pytest.raises(MoneyError):
        freeze(facts, command)


@pytest.mark.parametrize("value", [1.5, float("nan"), float("inf"), b"bytes", {1: "value"}, {1, 2}])
def test_unreviewed_metadata_is_not_coerced_or_serialized(value):
    facts, command = source()
    facts.document.issuer_snapshot["unexpected_number"] = value
    with pytest.raises(MoneyError):
        freeze(facts, command)


def test_oversized_metadata_or_normalized_copy_bypass_fails_without_rewriting_input():
    facts, command = source()
    facts.document.issuer_snapshot["oversized"] = "N" * (4 * 1024 * 1024)
    with pytest.raises(MoneyError):
        freeze(facts, command)
    facts, command = source()
    facts.document.lines[0].description = "  Needs normalization  "
    with pytest.raises(MoneyError):
        freeze(facts, command)
    assert facts.document.lines[0].description == "  Needs normalization  "
