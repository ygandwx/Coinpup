"""Exchange principal and explicit fees remain separate, exact and atomic."""

from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal

import pytest
from coinpup_api.ledger.models import CommandReceipt, FinancialOperation, Journal, JournalLine
from coinpup_api.ledger.posting import PostingService
from coinpup_api.ledger.posting_schemas import (
    ExchangeCreate,
    ExchangeResponse,
    ExpenseCreate,
    IncomeCreate,
    OpeningCreate,
    TransferCreate,
)
from coinpup_api.ledger.schemas import (
    AccountCreate,
    AccountUpdate,
    AssetUpdate,
    CategoryUpdate,
    EntityCreate,
    EntityUpdate,
)
from coinpup_api.ledger.service import LedgerError, LedgerService
from sqlalchemy import func, select
from sqlalchemy.orm import Session

pytestmark = pytest.mark.integration


@pytest.fixture
def exchange_setup(structure_database):
    engine, owner = structure_database
    structure, posting = LedgerService(engine), PostingService(engine)
    entity = structure.create_entity(
        owner,
        EntityCreate(
            kind="personal",
            name="Fictional FX owner",
            base_asset_id="USD",
            template_key="personal_default",
        ),
    )
    accounts = [
        structure.create_account(
            owner,
            entity.ledger.id,
            AccountCreate(
                name=f"Fictional wallet {index}",
                kind="wise",
                asset_ids=["USD", "EUR", "GBP", "BTC", "ETH"],
            ),
        )
        for index in range(2)
    ]
    categories = structure.list_categories(owner, entity.ledger.id)
    return {
        "engine": engine,
        "owner": owner,
        "structure": structure,
        "posting": posting,
        "entity": entity,
        "ledger": entity.ledger.id,
        "account": accounts[0],
        "other": accounts[1],
        "expense": next(item for item in categories if item.kind == "expense"),
        "income": next(item for item in categories if item.kind == "income"),
    }


def charge(s, amount="2", asset="USD", **changes):
    result = {
        "account_id": s["account"].id,
        "asset_id": asset,
        "amount": amount,
        "category_id": s["expense"].id,
    }
    result.update(changes)
    return result


def exchange(s, **changes):
    result = {
        "source_account_id": s["account"].id,
        "source_asset_id": "USD",
        "source_amount": "100",
        "destination_account_id": s["other"].id,
        "destination_asset_id": "EUR",
        "destination_amount": "90",
        "transaction_date": "2026-01-02",
    }
    result.update(changes)
    return ExchangeCreate(**result)


def opening(s, amount="1000", asset="USD", account=None):
    account = account or s["account"].id
    return s["posting"].post_opening(
        s["owner"],
        s["ledger"],
        OpeningCreate(
            account_id=account, asset_id=asset, amount=amount, transaction_date="2026-01-01"
        ),
        f"open-{account}-{asset}",
    )


def balances(s):
    return {
        (item.account_id, item.asset_id): item.amount
        for item in s["posting"].balances(s["owner"], s["ledger"])
    }


def counts(s):
    with Session(s["engine"]) as session:
        return tuple(
            session.scalar(select(func.count()).select_from(table))
            for table in (FinancialOperation, Journal, JournalLine, CommandReceipt)
        )


def expense(s, amount="0.1", asset="BTC", **changes):
    result = {
        "account_id": s["account"].id,
        "asset_id": asset,
        "amount": amount,
        "transaction_date": "2026-01-02",
        "recognition_date": "2026-01-02",
        "splits": [{"category_id": s["expense"].id, "amount": amount}],
    }
    result.update(changes)
    return ExpenseCreate(**result)


def test_a05_exchange_principal_and_fee_are_separate_actual_quantities(exchange_setup):
    s = exchange_setup
    opening(s)
    request = exchange(s, fees=[charge(s)])
    receipt = s["posting"].post_exchange(s["owner"], s["ledger"], request, "a05")
    assert isinstance(receipt, ExchangeResponse)
    assert receipt.source_amount == "100.00" and receipt.destination_amount == "90.00"
    assert receipt.fees[0].amount == "2.00"
    current = balances(s)
    assert current[s["account"].id, "USD"] == "898.00"
    assert current[s["other"].id, "EUR"] == "90.00"
    state = s["posting"].get_operation(s["owner"], s["ledger"], receipt.id)
    assert state.latest_posting == receipt and state.status == "active" and state.version == 1
    assert s["posting"].post_exchange(s["owner"], s["ledger"], request, "a05") == receipt
    with Session(s["engine"]) as session:
        assert session.scalar(
            select(func.sum(JournalLine.amount)).where(JournalLine.role == "expense")
        ) == Decimal("2")
        rows = session.execute(
            select(JournalLine.component_no, func.count())
            .where(JournalLine.journal_id == receipt.journal_id)
            .group_by(JournalLine.component_no)
        ).all()
        assert dict(rows) == {0: 4, 1: 2}
        assert all(
            value == 0
            for value in session.scalars(
                select(func.sum(JournalLine.amount))
                .where(JournalLine.journal_id == receipt.journal_id)
                .group_by(JournalLine.asset_id, JournalLine.component_no)
            )
        )


def test_a07_btc_payment_and_network_fee_preserve_principal_and_precision(exchange_setup):
    s = exchange_setup
    opening(s, "1", "BTC")
    request = expense(s, fees=[charge(s, "0.00001", "BTC")])
    receipt = s["posting"].post_expense(s["owner"], s["ledger"], request, "a07")
    assert receipt.amount == "0.10000000" and receipt.splits[0].amount == "0.10000000"
    assert receipt.fees[0].amount == "0.00001000"
    assert balances(s)[s["account"].id, "BTC"] == "0.89999000"
    assert s["posting"].get_operation(s["owner"], s["ledger"], receipt.id).latest_posting == receipt
    assert s["posting"].post_expense(s["owner"], s["ledger"], request, "a07") == receipt


@pytest.mark.parametrize(
    "fee_asset,fee_account,expected", [("EUR", "other", "88.00"), ("GBP", "account", "8.00")]
)
def test_fee_can_use_destination_asset_or_a_third_asset(
    exchange_setup, fee_asset, fee_account, expected
):
    s = exchange_setup
    opening(s)
    if fee_asset == "GBP":
        opening(s, "10", "GBP")
    request = exchange(s, fees=[charge(s, "2", fee_asset, account_id=s[fee_account].id)])
    receipt = s["posting"].post_exchange(s["owner"], s["ledger"], request, "different-fee-asset")
    current = balances(s)
    assert current[s["account"].id, "USD"] == "900.00"
    assert current[s[fee_account].id, fee_asset] == expected
    assert receipt.destination_amount == "90.00" and receipt.fees[0].asset_id == fee_asset


def test_exchange_can_move_two_assets_within_one_multiasset_account(exchange_setup):
    s = exchange_setup
    opening(s)
    request = exchange(s, destination_account_id=s["account"].id, fees=[charge(s)])
    receipt = s["posting"].post_exchange(s["owner"], s["ledger"], request, "one-wallet")
    assert receipt.source_account_id == receipt.destination_account_id
    assert balances(s)[s["account"].id, "USD"] == "898.00"
    assert balances(s)[s["account"].id, "EUR"] == "90.00"
    assert s["posting"].get_operation(s["owner"], s["ledger"], receipt.id).latest_posting == receipt


def test_fee_can_debit_a_separate_account_and_third_asset(exchange_setup):
    s = exchange_setup
    third = s["structure"].create_account(
        s["owner"],
        s["ledger"],
        AccountCreate(name="Fictional fee wallet", kind="cash", asset_ids=["GBP"]),
    )
    opening(s)
    opening(s, "10", "GBP", third.id)
    request = exchange(s, fees=[charge(s, "2", "GBP", account_id=third.id)])
    receipt = s["posting"].post_exchange(s["owner"], s["ledger"], request, "separate-fee-wallet")
    assert receipt.fees[0].account_id == third.id
    current = balances(s)
    assert current[s["account"].id, "USD"] == "900.00"
    assert current[s["other"].id, "EUR"] == "90.00"
    assert current[third.id, "GBP"] == "8.00"
    assert current[s["account"].id, "GBP"] == "0.00"


@pytest.mark.parametrize(
    "inactive,code",
    [
        ("account", "account_archived"),
        ("link", "account_asset_disabled"),
        ("asset", "asset_disabled"),
        ("category", "category_archived"),
    ],
)
def test_fee_only_inactive_targets_block_the_entire_exchange(exchange_setup, inactive, code):
    s = exchange_setup
    third = s["structure"].create_account(
        s["owner"],
        s["ledger"],
        AccountCreate(name="Fictional fee wallet", kind="cash", asset_ids=["GBP", "ETH"]),
    )
    opening(s)
    opening(s, "10", "GBP", third.id)
    if inactive == "account":
        s["structure"].update_account(
            s["owner"], s["ledger"], third.id, AccountUpdate(expected_version=1, archived=True)
        )
    elif inactive == "link":
        s["structure"].update_account(
            s["owner"], s["ledger"], third.id, AccountUpdate(expected_version=1, asset_ids=["ETH"])
        )
    elif inactive == "asset":
        s["structure"].update_asset(
            s["owner"], "GBP", AssetUpdate(expected_version=1, enabled=False)
        )
    else:
        s["structure"].update_category(
            s["owner"],
            s["ledger"],
            s["expense"].id,
            CategoryUpdate(expected_version=1, archived=True),
        )
    before, old = counts(s), balances(s)
    with pytest.raises(LedgerError) as error:
        s["posting"].post_exchange(
            s["owner"],
            s["ledger"],
            exchange(s, fees=[charge(s, "2", "GBP", account_id=third.id)]),
            "inactive-fee",
        )
    assert error.value.code == code
    assert counts(s) == before and balances(s) == old


def test_net_balance_check_accepts_incoming_amount_offset_by_same_asset_fee(exchange_setup):
    s = exchange_setup
    maximum = "99999999999999999999.99"
    opening(s)
    opening(s, maximum, "EUR", s["other"].id)
    request = exchange(s, fees=[charge(s, "90", "EUR", account_id=s["other"].id)])
    receipt = s["posting"].post_exchange(s["owner"], s["ledger"], request, "net-not-transient")
    assert receipt.destination_amount == receipt.fees[0].amount == "90.00"
    assert balances(s)[s["other"].id, "EUR"] == maximum
    assert balances(s)[s["account"].id, "USD"] == "900.00"


@pytest.mark.parametrize(
    "amount,asset,code",
    [
        ("0", "USD", "amount_positive"),
        ("-1", "USD", "amount_positive"),
        ("0.001", "USD", "amount_precision"),
        ("0.0000000000000000001", "ETH", "amount_precision"),
    ],
)
def test_invalid_fee_rolls_back_all_principal_and_fee_records(exchange_setup, amount, asset, code):
    s = exchange_setup
    opening(s)
    before, old = counts(s), balances(s)
    with pytest.raises(LedgerError) as error:
        s["posting"].post_exchange(
            s["owner"], s["ledger"], exchange(s, fees=[charge(s, amount, asset)]), "bad-fee"
        )
    assert error.value.code == code and counts(s) == before and balances(s) == old


@pytest.mark.parametrize(
    "field,value,code",
    [
        ("source_amount", "0", "amount_positive"),
        ("destination_amount", "-1", "amount_positive"),
        ("source_amount", "100.001", "amount_precision"),
        ("destination_amount", "90.001", "amount_precision"),
    ],
)
def test_invalid_exchange_principal_rolls_back_fees_too(exchange_setup, field, value, code):
    s = exchange_setup
    with pytest.raises(LedgerError) as error:
        s["posting"].post_exchange(
            s["owner"],
            s["ledger"],
            exchange(s, **{field: value, "fees": [charge(s)]}),
            "bad-principal",
        )
    assert error.value.code == code and counts(s) == (0, 0, 0, 0)


def test_fee_account_category_and_kind_must_belong_to_this_ledger(exchange_setup):
    s = exchange_setup
    other = s["structure"].create_entity(
        s["owner"],
        EntityCreate(
            kind="personal", name="Foreign", base_asset_id="USD", template_key="personal_default"
        ),
    )
    account = s["structure"].create_account(
        s["owner"], other.ledger.id, AccountCreate(name="Foreign", kind="cash", asset_ids=["USD"])
    )
    category = next(
        item
        for item in s["structure"].list_categories(s["owner"], other.ledger.id)
        if item.kind == "expense"
    )
    for fees, code in [
        ([charge(s, account_id=account.id)], "not_found"),
        ([charge(s, category_id=category.id)], "not_found"),
        ([charge(s, category_id=s["income"].id)], "category_kind_mismatch"),
    ]:
        with pytest.raises(LedgerError) as error:
            s["posting"].post_exchange(
                s["owner"], s["ledger"], exchange(s, fees=fees), "foreign-fee"
            )
        assert error.value.code == code
    assert counts(s) == (0, 0, 0, 0)


def test_fee_net_balance_overflow_prevents_principal_transfer(exchange_setup):
    s = exchange_setup
    opening(s, "-99999999999999999999.99", "GBP")
    before, old = counts(s), balances(s)
    with pytest.raises(LedgerError) as error:
        s["posting"].post_exchange(
            s["owner"], s["ledger"], exchange(s, fees=[charge(s, "0.01", "GBP")]), "overflow-fee"
        )
    assert error.value.code == "amount_range" and counts(s) == before and balances(s) == old


@pytest.mark.parametrize("kind", ["income", "expense", "transfer"])
def test_fees_do_not_change_legacy_principal_amount_or_classification(kind, exchange_setup):
    s = exchange_setup
    opening(s, "100")
    fee = charge(s)
    common = {"asset_id": "USD", "amount": "10", "transaction_date": "2026-01-02", "fees": [fee]}
    if kind == "transfer":
        request = TransferCreate(
            source_account_id=s["account"].id, destination_account_id=s["other"].id, **common
        )
    else:
        category = s["income"] if kind == "income" else s["expense"]
        request = (IncomeCreate if kind == "income" else ExpenseCreate)(
            account_id=s["account"].id,
            recognition_date="2026-01-02",
            splits=[{"category_id": category.id, "amount": "10"}],
            **common,
        )
    receipt = getattr(s["posting"], "post_" + kind)(
        s["owner"], s["ledger"], request, "legacy-with-fees"
    )
    assert receipt.amount == "10.00" and receipt.fees[0].amount == "2.00"
    assert balances(s)[s["account"].id, "USD"] == ("108.00" if kind == "income" else "88.00")
    assert s["posting"].get_operation(s["owner"], s["ledger"], receipt.id).latest_posting == receipt
    assert (
        getattr(s["posting"], "post_" + kind)(s["owner"], s["ledger"], request, "legacy-with-fees")
        == receipt
    )


def test_old_no_fee_request_and_explicit_empty_fees_replay_same_persisted_receipt(
    legacy_v1_receipts,
):
    s = legacy_v1_receipts
    request = ExpenseCreate.model_validate(s["cases"]["expense"]["body"])
    receipt = s["posting"].post_expense(s["owner"], s["ledger"], request, "legacy-empty")
    explicit = request.model_copy(update={"fees": []})
    assert (
        PostingService(s["engine"]).post_expense(s["owner"], s["ledger"], explicit, "legacy-empty")
        == receipt
    )
    with Session(s["engine"]) as session:
        stored = session.get(CommandReceipt, (s["ledger"], "legacy-empty"))
        assert "fees" not in stored.response and receipt.model_dump(mode="json") == stored.response
    with pytest.raises(LedgerError) as error:
        s["posting"].post_expense(
            s["owner"],
            s["ledger"],
            ExpenseCreate(**{**request.model_dump(), "fees": [charge(s)]}),
            "legacy-empty",
        )
    assert error.value.code == "idempotency_conflict"


def test_exchange_and_fee_receipt_replay_survive_disable_archive_and_restart(exchange_setup):
    s = exchange_setup
    opening(s)
    request = exchange(s, fees=[charge(s)])
    receipt = s["posting"].post_exchange(s["owner"], s["ledger"], request, "persistent-exchange")
    for account in (s["account"], s["other"]):
        s["structure"].update_account(
            s["owner"],
            s["ledger"],
            account.id,
            AccountUpdate(expected_version=1, archived=True, asset_ids=["ETH"]),
        )
    s["structure"].update_asset(s["owner"], "USD", AssetUpdate(expected_version=1, enabled=False))
    s["structure"].update_entity(
        s["owner"], s["entity"].id, EntityUpdate(expected_version=1, archived=True)
    )
    fresh, before = PostingService(s["engine"]), counts(s)
    assert fresh.post_exchange(s["owner"], s["ledger"], request, "persistent-exchange") == receipt
    assert fresh.get_operation(s["owner"], s["ledger"], receipt.id).latest_posting == receipt
    assert fresh.list_operations(s["owner"], s["ledger"])[0].latest_posting == receipt
    assert balances(s)[s["account"].id, "USD"] == "898.00" and counts(s) == before
    with pytest.raises(LedgerError) as error:
        fresh.post_exchange(
            s["owner"],
            s["ledger"],
            request.model_copy(update={"destination_amount": "91"}),
            "persistent-exchange",
        )
    assert error.value.code == "idempotency_conflict"


def test_concurrent_exchange_retries_create_principal_and_fees_once(exchange_setup):
    s = exchange_setup
    opening(s)
    request = exchange(s, fees=[charge(s), charge(s, "1", "GBP")])

    def post(_):
        return PostingService(s["engine"]).post_exchange(
            s["owner"], s["ledger"], request, "concurrent-fx"
        )

    with ThreadPoolExecutor(max_workers=3) as pool:
        receipts = list(pool.map(post, range(3)))
    assert all(item == receipts[0] for item in receipts)
    assert counts(s) == (2, 2, 10, 2)
    assert balances(s)[s["account"].id, "USD"] == "898.00"
    assert balances(s)[s["account"].id, "GBP"] == "-1.00"
