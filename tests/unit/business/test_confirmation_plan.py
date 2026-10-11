"""Fictional reviewed invoices: gross amounts, original dates and corruption boundaries."""

from dataclasses import FrozenInstanceError
from datetime import UTC, date, datetime
from decimal import localcontext
from uuid import uuid4

import pytest
from coinpup_api.business.confirmation_plan import plan_confirmation
from coinpup_api.business.draft_schemas import BusinessDraftLineResponse, BusinessDraftResponse
from coinpup_api.ledger.assets import ASSETS
from coinpup_api.ledger.errors import MoneyError


def draft(kind="bill"):
    ledger, identifier, party = uuid4(), uuid4(), uuid4()
    stamp = datetime(2026, 10, 11, tzinfo=UTC)
    common = dict(ledger_id=ledger, version=4, created_at=stamp, updated_at=stamp, archived=False)
    line = BusinessDraftLineResponse(
        **common,
        id=uuid4(),
        document_id=identifier,
        line_no=2,
        asset_id="USD",
        description="Fictional 经营项目",
        quantity="1.00",
        unit_price="100.00",
        discount_amount="0",
        tax_rate_percent="10",
        category_id=uuid4(),
        project_id=uuid4(),
        recognition_date=date(2026, 9, 30),
        category_kind="expense" if kind == "bill" else "income",
        category_snapshot={"name": "Fictional category"},
        project_snapshot={"name": "Fictional project"},
        net_amount="100.00",
        tax_amount="10.00",
        total_amount="110.00",
    )
    return BusinessDraftResponse(
        **common,
        id=identifier,
        document_kind=kind,
        party_id=party,
        asset_id="USD",
        issue_date=date(2026, 10, 11),
        due_date=date(2026, 11, 1),
        state="draft",
        issuer_snapshot={"name": "Fictional issuer"},
        party_snapshot={"name": "Fictional party"},
        line_count=1,
        net_amount="100.00",
        tax_amount="10.00",
        total_amount="110.00",
        lines=[line],
    )


@pytest.mark.parametrize(
    "kind,control,account,category",
    [
        ("bill", "payable.supplier", "-110.00", "110.00"),
        ("invoice", "receivable.customer", "110.00", "-110.00"),
    ],
)
def test_gross_accrual_has_no_second_tax_expense_and_preserves_reviewed_source(
    kind, control, account, category
):
    source = draft(kind)
    original = source.model_dump(mode="json")
    with localcontext() as context:
        context.prec = 2
        result = plan_confirmation(source, ASSETS["USD"])
    row = result.lines[0]
    assert result.control_key == control and result.operation_kind == kind + "_accrual"
    assert row.account_amount.to_string() == account
    assert row.category_amount.to_string() == category
    assert (row.account_amount + row.category_amount).minor_units == 0
    assert result.price.net.to_string() == "100.00" and result.price.tax.to_string() == "10.00"
    assert result.transaction_date == date(2026, 10, 11)
    assert result.recognition_dates == (date(2026, 9, 30),)
    assert (row.line_id, row.project_id, row.category_id) == (
        source.lines[0].id,
        source.lines[0].project_id,
        source.lines[0].category_id,
    )
    assert source.model_dump(mode="json") == original
    with pytest.raises(FrozenInstanceError):
        result.source_version = 10


def test_zero_line_date_is_checked_but_it_does_not_make_a_zero_journal():
    source = draft()
    zero = source.lines[0].model_copy(
        update=dict(
            id=uuid4(),
            line_no=1,
            unit_price="0",
            net_amount="0.00",
            tax_amount="0.00",
            total_amount="0.00",
            recognition_date=date(2026, 8, 1),
        )
    )
    source = source.model_copy(update=dict(lines=[source.lines[0], zero], line_count=2))
    result = plan_confirmation(source, ASSETS["USD"])
    assert len(result.lines) == 1 and len(result.price.lines) == 2
    assert result.lines[0].line_no == 2
    assert result.recognition_dates == (date(2026, 8, 1), date(2026, 9, 30))


@pytest.mark.parametrize(
    "changes",
    [
        {"ledger_id": uuid4()},
        {"document_id": uuid4()},
        {"asset_id": "EUR"},
        {"category_kind": "income"},
        {"archived": True},
        {"line_no": 0},
        {"line_no": True},
        {"net_amount": "99.00"},
        {"tax_amount": "0.00"},
        {"total_amount": "100.00"},
        {"unit_price": "99.00"},
    ],
)
def test_invalid_or_changed_line_never_produces_a_confirmation(changes):
    source = draft()
    source = source.model_copy(update={"lines": [source.lines[0].model_copy(update=changes)]})
    with pytest.raises(MoneyError, match="reviewed draft"):
        plan_confirmation(source, ASSETS["USD"])


@pytest.mark.parametrize(
    "changes",
    [
        {"line_count": 2},
        {"asset_id": "EUR"},
        {"state": "confirmed"},
        {"document_kind": "other"},
        {"net_amount": "90.00"},
        {"tax_amount": "20.00"},
        {"total_amount": "100.00"},
    ],
)
def test_inconsistent_document_summary_is_not_trusted(changes):
    with pytest.raises(MoneyError):
        plan_confirmation(draft().model_copy(update=changes), ASSETS["USD"])


@pytest.mark.parametrize("duplicate", ["id", "line_no"])
def test_duplicate_line_identity_or_position_is_rejected(duplicate):
    source = draft()
    second = source.lines[0].model_copy(update={"id": uuid4(), "line_no": 3})
    setattr(second, duplicate, getattr(source.lines[0], duplicate))
    with pytest.raises(MoneyError):
        plan_confirmation(
            source.model_copy(update={"lines": [*source.lines, second], "line_count": 2}),
            ASSETS["USD"],
        )


def test_empty_and_all_zero_documents_cannot_be_confirmed():
    source = draft()
    amounts = dict(net_amount="0.00", tax_amount="0.00", total_amount="0.00")
    zero = source.lines[0].model_copy(update={**amounts, "unit_price": "0"})
    for lines in ([], [zero]):
        with pytest.raises(MoneyError) as error:
            plan_confirmation(
                source.model_copy(update={**amounts, "lines": lines, "line_count": len(lines)}),
                ASSETS["USD"],
            )
        assert error.value.code == "confirmation_total_positive"


def test_archived_draft_requires_explicit_restore():
    with pytest.raises(MoneyError) as error:
        plan_confirmation(draft().model_copy(update={"archived": True}), ASSETS["USD"])
    assert error.value.code == "document_archived"


def test_two_positive_lines_keep_distinct_dates_and_stable_line_order():
    source = draft("invoice")
    first = source.lines[0].model_copy(
        update={"id": uuid4(), "line_no": 1, "recognition_date": date(2026, 8, 31)}
    )
    source = source.model_copy(
        update={
            "lines": [source.lines[0], first],
            "line_count": 2,
            "net_amount": "200.00",
            "tax_amount": "20.00",
            "total_amount": "220.00",
        }
    )
    result = plan_confirmation(source, ASSETS["USD"])
    assert [row.line_no for row in result.lines] == [1, 2]
    assert [row.recognition_date for row in result.lines] == [date(2026, 8, 31), date(2026, 9, 30)]
    assert sum(row.account_amount.minor_units for row in result.lines) == 22000
    assert result.price.tax.to_string() == "20.00"


@pytest.mark.parametrize("count", [200, 201])
def test_confirmation_preserves_the_existing_document_size_bound(count):
    source = draft()
    lines = [
        source.lines[0].model_copy(update={"id": uuid4(), "line_no": i + 1}) for i in range(count)
    ]
    source = source.model_copy(
        update={
            "lines": lines,
            "line_count": count,
            "net_amount": f"{count * 100}.00",
            "tax_amount": f"{count * 10}.00",
            "total_amount": f"{count * 110}.00",
        }
    )
    if count == 200:
        result = plan_confirmation(source, ASSETS["USD"])
        assert len(result.lines) == 200 and result.price.total.to_string() == "22000.00"
    else:
        with pytest.raises(MoneyError) as error:
            plan_confirmation(source, ASSETS["USD"])
        assert error.value.code == "pricing_lines"


@pytest.mark.parametrize(
    "asset_id,value",
    [
        ("USD", "0.01"),
        ("USD", "99999999999999999999.99"),
        ("BTC", "0.00000001"),
    ],
)
def test_exact_asset_extremes_and_pair_balance(asset_id, value):
    asset = ASSETS[asset_id]
    source = draft("invoice")
    line = source.lines[0].model_copy(
        update={
            "asset_id": asset_id,
            "unit_price": value,
            "tax_rate_percent": "0",
            "net_amount": value,
            "tax_amount": "0",
            "total_amount": value,
        }
    )
    source = source.model_copy(
        update={
            "asset_id": asset_id,
            "lines": [line],
            "net_amount": value,
            "tax_amount": "0",
            "total_amount": value,
        }
    )
    with localcontext() as context:
        context.prec = 2
        result = plan_confirmation(source, asset)
    row = result.lines[0]
    assert row.account_amount.to_string() == value
    assert (row.account_amount + row.category_amount).minor_units == 0


def test_confirmation_does_not_round_an_over_precision_source_price():
    source = draft()
    source = source.model_copy(
        update={
            "lines": [source.lines[0].model_copy(update={"unit_price": "100.001"})],
        }
    )
    with pytest.raises(MoneyError):
        plan_confirmation(source, ASSETS["USD"])
