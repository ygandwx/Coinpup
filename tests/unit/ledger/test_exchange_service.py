"""Published hash compatibility, separate fees and exact exchange command boundaries."""

from dataclasses import replace
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from coinpup_api.ledger.assets import get_asset
from coinpup_api.ledger.errors import MoneyError
from coinpup_api.ledger.money import Amount
from coinpup_api.ledger.posting import PostingService, command_hash
from coinpup_api.ledger.posting_schemas import (
    ExchangeCreate,
    ExchangeResponse,
    ExpenseCreate,
    FeeCreate,
    FeeResponse,
    FinancialResponse,
    IncomeCreate,
    OpeningCreate,
    OperationResponse,
    TransferCreate,
    TransferResponse,
)
from coinpup_api.ledger.posting_storage import PostingLine, prepare_journal_lines
from pydantic import TypeAdapter, ValidationError

LEDGER = UUID("00000000-0000-4000-8000-000000000001")
ACCOUNT = UUID("00000000-0000-4000-8000-000000000002")
DESTINATION = UUID("00000000-0000-4000-8000-000000000003")
CATEGORY = UUID("00000000-0000-4000-8000-000000000004")
GOLDEN_HASHES = {
    "opening": "a6e6dc2ade58227c9911858f1d903f0f810fd8d3533a377c7c13898a287409f8",
    "income": "a83f650fd920b2ebe39a3c283515f05f5441c20372aa27665ef7519238ac18fc",
    "expense": "618cbcd9c79df2b968c540a8620de93df8d01c8d307b687e34fabe62a15d94d4",
    "transfer": "632d0f0ceec98d344a89447f0bd494dc846df3cf60b6a63d5b3d0592b1466eae",
}


def legacy_command(kind, **changes):
    data = {"asset_id": "USD", "amount": "10.00", "transaction_date": "2026-01-02"}
    cls = {
        "opening": OpeningCreate,
        "income": IncomeCreate,
        "expense": ExpenseCreate,
        "transfer": TransferCreate,
    }[kind]
    if kind == "transfer":
        data.update(source_account_id=ACCOUNT, destination_account_id=DESTINATION)
    else:
        data["account_id"] = ACCOUNT
    if kind in {"income", "expense"}:
        data.update(
            recognition_date="2026-01-01", splits=[{"category_id": CATEGORY, "amount": "10.00"}]
        )
    data.update(changes)
    return cls(**data)


def fee(**changes):
    data = {"account_id": ACCOUNT, "asset_id": "USD", "amount": "2.00", "category_id": CATEGORY}
    data.update(changes)
    return FeeCreate(**data)


def exchange(**changes):
    data = {
        "source_account_id": ACCOUNT,
        "source_asset_id": "USD",
        "source_amount": "100.00",
        "destination_account_id": DESTINATION,
        "destination_asset_id": "EUR",
        "destination_amount": "90.00",
        "transaction_date": "2026-01-02",
    }
    data.update(changes)
    return ExchangeCreate(**data)


@pytest.mark.parametrize("kind", GOLDEN_HASHES)
def test_pre_fee_published_golden_hashes_are_unchanged(kind):
    original = legacy_command(kind)
    assert command_hash(kind, LEDGER, original) == GOLDEN_HASHES[kind]
    if kind != "opening":
        assert command_hash(kind, LEDGER, legacy_command(kind, fees=[])) == GOLDEN_HASHES[kind]
        charged = legacy_command(kind, fees=[fee()])
        assert command_hash(kind, LEDGER, charged) != GOLDEN_HASHES[kind]
        assert command_hash(kind, LEDGER, charged) == command_hash(
            kind, LEDGER, type(charged).model_validate_json(charged.model_dump_json())
        )


def test_exchange_same_account_is_valid_but_same_asset_is_not():
    assert exchange(destination_account_id=ACCOUNT).source_account_id == ACCOUNT
    with pytest.raises(ValidationError):
        exchange(destination_asset_id="USD")
    with pytest.raises(ValidationError):
        exchange(recognition_date="2026-01-01")
    with pytest.raises(ValidationError):
        exchange(transaction_date=0)


@pytest.mark.parametrize(
    "amount", [1, 1.0, True, "1e2", "NaN", "1.000000000000000000001" + "0" * 30]
)
def test_exchange_and_fees_require_bounded_decimal_strings(amount):
    with pytest.raises(ValidationError):
        exchange(source_amount=amount)
    with pytest.raises(ValidationError):
        fee(amount=amount)


def test_fee_count_bound_and_opening_does_not_accept_fees():
    assert len(exchange(fees=[fee()] * 20).fees) == 20
    for kind in ("income", "expense", "transfer"):
        with pytest.raises(ValidationError):
            legacy_command(kind, fees=[fee()] * 21)
    with pytest.raises(ValidationError):
        exchange(fees=[fee()] * 21)
    with pytest.raises(ValidationError):
        legacy_command("opening", fees=[])


@pytest.mark.parametrize("kind", ["opening", "income", "expense", "transfer"])
def test_old_receipt_shape_omits_empty_fees_in_json_python_and_union(kind):
    original = {
        "id": str(uuid4()),
        "ledger_id": str(LEDGER),
        "journal_id": str(uuid4()),
        "kind": kind,
        "version": 1,
        "asset_id": "USD",
        "amount": "10.00",
        "transaction_date": "2026-01-02",
        "recognition_date": "2026-01-02",
        "description": "",
        "created_at": "2026-01-02T00:00:00Z",
    }
    if kind == "transfer":
        original.update(source_account_id=str(ACCOUNT), destination_account_id=str(DESTINATION))
    else:
        original.update(account_id=str(ACCOUNT), splits=[])
    restored = TypeAdapter(FinancialResponse).validate_python(original)
    assert restored.model_dump(mode="json") == original and "fees" not in restored.model_dump()
    assert TypeAdapter(FinancialResponse).validate_json(restored.model_dump_json()).fees == []
    if kind != "opening":
        charged = restored.model_copy(update={"fees": [FeeResponse(**fee().model_dump())]})
        assert charged.model_dump(mode="json")["fees"][0]["amount"] == "2.00"
    assert isinstance(restored, TransferResponse if kind == "transfer" else OperationResponse)


def test_new_exchange_receipt_is_a_distinct_union_member():
    response = ExchangeResponse(
        id=uuid4(),
        ledger_id=LEDGER,
        journal_id=uuid4(),
        kind="exchange",
        version=1,
        source_account_id=ACCOUNT,
        source_asset_id="USD",
        source_amount="100.00",
        destination_account_id=ACCOUNT,
        destination_asset_id="EUR",
        destination_amount="90.00",
        transaction_date="2026-01-02",
        recognition_date="2026-01-02",
        description="",
        created_at=datetime.now(UTC),
        fees=[FeeResponse(**fee().model_dump())],
    )
    assert TypeAdapter(FinancialResponse).validate_json(response.model_dump_json()) == response
    assert "amount" not in response.model_dump() and response.fees[0].amount == "2.00"


def test_prebind_components_cannot_cross_subsidize_principal_with_fees():
    asset = get_asset("USD")
    journal = uuid4()
    lines = [
        PostingLine(journal, LEDGER, 1, "account", "USD", Amount(asset, -100), account_id=ACCOUNT),
        PostingLine(journal, LEDGER, 2, "expense", "USD", Amount(asset, 200), category_id=CATEGORY),
        PostingLine(
            journal,
            LEDGER,
            3,
            "account",
            "USD",
            Amount(asset, -200),
            account_id=ACCOUNT,
            component_no=1,
        ),
        PostingLine(
            journal,
            LEDGER,
            4,
            "expense",
            "USD",
            Amount(asset, 100),
            category_id=CATEGORY,
            component_no=1,
        ),
    ]
    with pytest.raises(MoneyError) as error:
        prepare_journal_lines(lines, {"USD": asset})
    assert error.value.code == "journal_unbalanced"


@pytest.mark.parametrize("component", [-1, True, 21, 2])
def test_prebind_fee_components_must_be_bounded_and_contiguous(component):
    asset = get_asset("USD")
    journal = uuid4()
    lines = [
        PostingLine(journal, LEDGER, 1, "account", "USD", Amount(asset, 100), account_id=ACCOUNT),
        PostingLine(journal, LEDGER, 2, "equity", "USD", Amount(asset, -100)),
        PostingLine(
            journal,
            LEDGER,
            3,
            "account",
            "USD",
            Amount(asset, -10),
            account_id=ACCOUNT,
            component_no=component,
        ),
        PostingLine(
            journal,
            LEDGER,
            4,
            "expense",
            "USD",
            Amount(asset, 10),
            category_id=CATEGORY,
            component_no=component,
        ),
    ]
    with pytest.raises(MoneyError) as error:
        prepare_journal_lines(lines, {"USD": asset})
    assert error.value.code == "journal_component"


def test_aggregate_balance_guard_checks_final_net_once_for_each_account_asset():
    asset = get_asset("USD")
    maximum = Amount.parse("99999999999999999999.99", asset)
    journal = uuid4()
    incoming = PostingLine(
        journal, LEDGER, 1, "account", "USD", Amount(asset, 100), account_id=ACCOUNT
    )
    fee_debit = replace(incoming, line_no=2, component_no=1, amount=Amount(asset, -100))
    service = PostingService(None)
    calls = []

    def current(_session, ledger, account, definition):
        calls.append((ledger, account, definition.asset_id))
        return maximum.minor_units

    service._current_units = current
    service._validate_balance_deltas(None, LEDGER, [incoming, fee_debit], {"USD": asset})
    assert calls == [(LEDGER, ACCOUNT, "USD")]
    with pytest.raises(MoneyError) as error:
        service._validate_balance_deltas(None, LEDGER, [incoming], {"USD": asset})
    assert error.value.code == "amount_range"
