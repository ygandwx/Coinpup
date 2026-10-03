"""Strict posting commands, stable hashes and exact row-aware write validation."""

from dataclasses import replace
from decimal import Decimal, Inexact, localcontext
from uuid import uuid4

import pytest
from coinpup_api.ledger.assets import get_asset, stablecoin_asset
from coinpup_api.ledger.errors import MoneyError
from coinpup_api.ledger.money import Amount
from coinpup_api.ledger.posting import PostingService, command_hash
from coinpup_api.ledger.posting_schemas import ExpenseCreate, IncomeCreate, OpeningCreate
from coinpup_api.ledger.posting_storage import PostingLine, prepare_journal_lines
from coinpup_api.ledger.service import LedgerError
from pydantic import ValidationError


def opening(**changes):
    values = {
        "account_id": uuid4(),
        "asset_id": "USD",
        "amount": "1.00",
        "transaction_date": "2026-01-01",
    }
    values.update(changes)
    return OpeningCreate(**values)


def classified(cls=ExpenseCreate, **changes):
    values = {
        "account_id": uuid4(),
        "asset_id": "USD",
        "amount": "1.00",
        "transaction_date": "2026-01-01",
        "recognition_date": "2025-12-31",
        "splits": [{"category_id": uuid4(), "amount": "1.00"}],
    }
    values.update(changes)
    return cls(**values)


@pytest.mark.parametrize(
    "amount",
    [
        1,
        1.0,
        True,
        "1e2",
        "NaN",
        "Infinity",
        "+1",
        " 1",
        "1 ",
        "01",
        ".1",
        "1.",
        "１.０",
        "1,000",
        "9" * 41,
    ],
)
def test_quantities_are_bounded_decimal_strings(amount):
    with pytest.raises(ValidationError):
        opening(amount=amount)


@pytest.mark.parametrize(
    "value", [0, 1735689600, "2026-01-01T00:00:00Z", "2026-02-30", "2026-1-1", None]
)
def test_posting_dates_are_explicit_calendar_dates(value):
    with pytest.raises(ValidationError):
        opening(transaction_date=value)
    with pytest.raises(ValidationError):
        classified(recognition_date=value)


@pytest.mark.parametrize("splits", [[], [{"category_id": uuid4(), "amount": "1"}] * 101])
def test_splits_are_nonempty_and_bounded(splits):
    with pytest.raises(ValidationError):
        classified(splits=splits)


def test_duplicate_categories_rejected_and_separate_dates_preserved():
    category = uuid4()
    with pytest.raises(ValidationError):
        classified(splits=[{"category_id": category, "amount": "0.50"}] * 2)
    payload = classified()
    assert str(payload.transaction_date) == "2026-01-01"
    assert str(payload.recognition_date) == "2025-12-31"
    with pytest.raises(ValidationError):
        opening(recognition_date="2026-01-01")
    with pytest.raises(ValidationError):
        opening(description="secret\x00invalid")


def test_hash_is_stable_for_identical_original_commands_without_generated_ids():
    ledger = uuid4()
    payload = opening()
    parsed = OpeningCreate.model_validate_json(payload.model_dump_json())
    assert command_hash("opening", ledger, payload) == command_hash("opening", ledger, parsed)
    assert command_hash("opening", ledger, payload) == command_hash(
        "opening", ledger, opening(**payload.model_dump())
    )
    assert command_hash("opening", ledger, payload) != command_hash("income", ledger, payload)
    assert command_hash("opening", ledger, payload) != command_hash("opening", uuid4(), payload)
    changed = payload.model_copy(update={"id": uuid4()})
    assert command_hash("opening", ledger, payload) != command_hash("opening", ledger, changed)
    equivalent_quantity = payload.model_copy(update={"amount": "1.0"})
    assert command_hash("opening", ledger, payload) != command_hash(
        "opening", ledger, equivalent_quantity
    )


@pytest.mark.parametrize("key", ["", "has space", "newline\n", "\t", "汉字", "a" * 129, None])
def test_invalid_idempotency_key_rejected_before_storage(key):
    with pytest.raises(LedgerError) as error:
        PostingService(None).post_opening(uuid4(), uuid4(), opening(), key)
    assert error.value.code == "invalid_idempotency_key"


def paired_lines(asset=None, amount="1.00"):
    asset = asset or get_asset("USD")
    quantity = Amount.parse(amount, asset)
    ledger, journal, account = uuid4(), uuid4(), uuid4()
    return [
        PostingLine(journal, ledger, 1, "account", asset.asset_id, quantity, account_id=account),
        PostingLine(journal, ledger, 2, "equity", asset.asset_id, -quantity),
    ]


def test_storage_boundary_is_exact_under_hostile_decimal_context():
    asset = get_asset("ETH")
    quantity = "99999999999999999999.999999999999999999"
    with localcontext() as context:
        context.prec = 2
        context.traps[Inexact] = True
        result = prepare_journal_lines(paired_lines(asset, quantity), {asset.asset_id: asset})
    assert result[0]["amount"] == Decimal(quantity)
    assert result[1]["amount"] == Decimal("-" + quantity)


@pytest.mark.parametrize(
    "value", ["1.00", Decimal("1.00"), 1, 1.0, True, Decimal("0.0000000000000000001")]
)
def test_storage_rejects_raw_quantities_before_returning_bind_parameters(value):
    lines = paired_lines()
    lines[0] = replace(lines[0], amount=value)
    with pytest.raises(MoneyError) as error:
        prepare_journal_lines(lines, {"USD": get_asset("USD")})
    assert error.value.code == "amount_type"


def test_storage_validates_actual_row_asset_and_complete_catalog_identity():
    lines = paired_lines()
    lines[0] = replace(lines[0], asset_id="EUR")
    with pytest.raises(MoneyError) as error:
        prepare_journal_lines(lines, {"USD": get_asset("USD"), "EUR": get_asset("EUR")})
    assert error.value.code == "asset_mismatch"
    six = stablecoin_asset(code="USDC", network="fictional", token_reference="Fake", scale=6)
    eighteen = stablecoin_asset(code="USDC", network="fictional", token_reference="Fake", scale=18)
    with pytest.raises(MoneyError) as error:
        prepare_journal_lines(paired_lines(six, "1"), {six.asset_id: eighteen})
    assert error.value.code == "asset_mismatch"


@pytest.mark.parametrize(
    "change,code",
    [
        ({"amount": Amount(get_asset("USD"), 0)}, "amount_zero"),
        ({"line_no": 1}, "journal_position"),
        ({"line_no": True}, "journal_position"),
        ({"ledger_id": uuid4()}, "journal_scope"),
        ({"journal_id": uuid4()}, "journal_scope"),
        ({"role": "account"}, "journal_role"),
        ({"amount": Amount(get_asset("USD"), -99)}, "journal_unbalanced"),
    ],
)
def test_storage_rejects_incomplete_or_inconsistent_journals(change, code):
    lines = paired_lines()
    lines[1] = replace(lines[1], **change)
    with pytest.raises(MoneyError) as error:
        prepare_journal_lines(lines, {"USD": get_asset("USD")})
    assert error.value.code == code
    with pytest.raises(MoneyError) as incomplete:
        prepare_journal_lines(lines[:1], {"USD": get_asset("USD")})
    assert incomplete.value.code == "journal_incomplete"


def test_storage_balances_each_asset_separately():
    dollar = paired_lines()
    euro = paired_lines(get_asset("EUR"))
    euro[0] = replace(
        euro[0], ledger_id=dollar[0].ledger_id, journal_id=dollar[0].journal_id, line_no=2
    )
    euro[1] = replace(
        euro[1], ledger_id=dollar[0].ledger_id, journal_id=dollar[0].journal_id, line_no=3
    )
    # Equal numbers in USD and EUR must never cancel one another.
    cross_currency = [dollar[0], euro[1]]
    with pytest.raises(MoneyError) as error:
        prepare_journal_lines(cross_currency, {"USD": get_asset("USD"), "EUR": get_asset("EUR")})
    assert error.value.code == "journal_unbalanced"


def test_classified_schemas_have_the_same_input_contract():
    expense = classified()
    income = IncomeCreate.model_validate(expense.model_dump())
    assert expense.model_dump() == income.model_dump()
