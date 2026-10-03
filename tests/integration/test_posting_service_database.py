"""Financial invariants on an explicitly configured disposable PostgreSQL database."""

from concurrent.futures import ThreadPoolExecutor
from datetime import date
from uuid import uuid4

import pytest
from coinpup_api.ledger.models import (
    CommandReceipt,
    FinancialOperation,
    Journal,
    JournalLine,
    OpeningPosition,
)
from coinpup_api.ledger.posting import PostingService
from coinpup_api.ledger.posting_schemas import ExpenseCreate, IncomeCreate, OpeningCreate
from coinpup_api.ledger.schemas import (
    AccountCreate,
    AccountUpdate,
    AssetCreate,
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
def ledger_setup(structure_database):
    engine, owner = structure_database
    structure, posting = LedgerService(engine), PostingService(engine)
    entity = structure.create_entity(
        owner,
        EntityCreate(
            kind="personal",
            name="Fictional financial test",
            base_asset_id="USD",
            template_key="personal_default",
        ),
    )
    account = structure.create_account(
        owner,
        entity.ledger.id,
        AccountCreate(
            name="Fictional Wise",
            kind="wise",
            asset_ids=["USD", "EUR", "BTC", "ETH", "XMR"],
        ),
    )
    categories = structure.list_categories(owner, entity.ledger.id)
    return {
        "engine": engine,
        "owner": owner,
        "structure": structure,
        "posting": posting,
        "entity": entity,
        "ledger": entity.ledger.id,
        "account": account,
        "expenses": [item for item in categories if item.kind == "expense"],
        "income": next(item for item in categories if item.kind == "income"),
    }


def opening(setup, amount="100.00", asset="USD", **changes):
    payload = {
        "account_id": setup["account"].id,
        "asset_id": asset,
        "amount": amount,
        "transaction_date": "2026-01-01",
        "description": "Fictional opening",
    }
    payload.update(changes)
    return OpeningCreate(**payload)


def classified(setup, kind="expense", amount="25.00", asset="USD", **changes):
    category = setup["expenses"][0] if kind == "expense" else setup["income"]
    payload = {
        "account_id": setup["account"].id,
        "asset_id": asset,
        "amount": amount,
        "transaction_date": "2026-02-01",
        "recognition_date": "2026-01-31",
        "description": "Fictional classified posting",
        "splits": [{"category_id": category.id, "amount": amount}],
    }
    payload.update(changes)
    return (ExpenseCreate if kind == "expense" else IncomeCreate)(**payload)


def quantities(setup):
    return {
        item.asset_id: item.amount
        for item in setup["posting"].balances(setup["owner"], setup["ledger"], setup["account"].id)
    }


def financial_counts(engine):
    with Session(engine) as session:
        return tuple(
            session.scalar(select(func.count()).select_from(table))
            for table in (FinancialOperation, Journal, JournalLine, OpeningPosition, CommandReceipt)
        )


def test_a03_exact_multiasset_balance_and_opening_not_income(ledger_setup):
    s = ledger_setup
    posting, owner, ledger = s["posting"], s["owner"], s["ledger"]
    first = posting.post_opening(owner, ledger, opening(s), "opening-usd")
    posting.post_opening(owner, ledger, opening(s, "80", "EUR"), "opening-eur")
    expense = classified(
        s,
        splits=[
            {"category_id": s["expenses"][0].id, "amount": "10"},
            {"category_id": s["expenses"][1].id, "amount": "15"},
        ],
    )
    spent = posting.post_expense(owner, ledger, expense, "expense-usd")
    posting.post_income(owner, ledger, classified(s, "income", "10", "EUR"), "income-eur")
    assert quantities(s) == {
        "USD": "75.00",
        "EUR": "90.00",
        "BTC": "0.00000000",
        "ETH": "0.000000000000000000",
        "XMR": "0.000000000000",
    }
    assert first.recognition_date == first.transaction_date == date(2026, 1, 1)
    assert spent.transaction_date == date(2026, 2, 1) and spent.recognition_date == date(
        2026, 1, 31
    )
    state = posting.get_operation(owner, ledger, spent.id)
    assert state.latest_posting == spent and state.status == "active" and state.version == 1
    with Session(s["engine"]) as session:
        opening_roles = session.scalars(
            select(JournalLine.role).where(JournalLine.journal_id == first.journal_id)
        ).all()
        assert set(opening_roles) == {"account", "equity"}
        assert (
            session.scalar(
                select(func.count()).select_from(Journal).where(Journal.sealed.is_(False))
            )
            == 0
        )
        assert all(
            total == 0
            for total in session.scalars(
                select(func.sum(JournalLine.amount)).group_by(
                    JournalLine.journal_id, JournalLine.asset_id
                )
            )
        )
    other = s["structure"].create_account(
        owner, ledger, AccountCreate(name="Untouched", kind="cash", asset_ids=["USD"])
    )
    assert posting.balances(owner, ledger, other.id)[0].amount == "0.00"


@pytest.mark.parametrize(
    "asset,amount",
    [
        ("BTC", "0.12345678"),
        ("ETH", "1.000000000000000001"),
        ("XMR", "0.123456789012"),
    ],
)
def test_a07_crypto_precision_survives_post_read_and_balance(ledger_setup, asset, amount):
    s = ledger_setup
    receipt = s["posting"].post_opening(
        s["owner"], s["ledger"], opening(s, amount, asset), "precision"
    )
    assert receipt.amount == amount
    assert (
        PostingService(s["engine"])
        .get_operation(s["owner"], s["ledger"], receipt.id)
        .latest_posting
        == receipt
    )
    assert quantities(s)[asset] == amount


def test_explicit_stablecoin_identity_and_six_digit_precision(ledger_setup):
    s = ledger_setup
    asset = s["structure"].create_asset(
        s["owner"],
        AssetCreate(
            code="USDC",
            kind="token",
            network="fictional",
            token_reference="FictionalToken",
            scale=6,
        ),
    )
    s["structure"].update_account(
        s["owner"],
        s["ledger"],
        s["account"].id,
        AccountUpdate(
            expected_version=1,
            asset_ids=[*s["account"].asset_ids, asset.asset_id],
        ),
    )
    income = s["posting"].post_income(
        s["owner"], s["ledger"], classified(s, "income", "0.000001", asset.asset_id), "token"
    )
    assert income.amount == "0.000001" and quantities(s)[asset.asset_id] == "0.000001"
    before = financial_counts(s["engine"])
    with pytest.raises(LedgerError) as error:
        s["posting"].post_income(
            s["owner"],
            s["ledger"],
            classified(s, "income", "0.0000001", asset.asset_id),
            "bad-token",
        )
    assert error.value.code == "amount_precision" and financial_counts(s["engine"]) == before


@pytest.mark.parametrize(
    "kind,amount,code",
    [
        ("opening", "0.00", "amount_positive"),
        ("expense", "0.00", "amount_positive"),
        ("income", "-1", "amount_positive"),
        ("expense", "-1", "amount_positive"),
        ("expense", "1.001", "amount_precision"),
        ("opening", "100000000000000000000", "amount_range"),
    ],
)
def test_invalid_amount_rolls_back_every_financial_record(ledger_setup, kind, amount, code):
    s = ledger_setup
    before = financial_counts(s["engine"])
    command = opening(s, amount) if kind == "opening" else classified(s, kind, amount)
    with pytest.raises(LedgerError) as error:
        getattr(s["posting"], "post_" + kind)(s["owner"], s["ledger"], command, "rejected")
    assert error.value.code == code
    assert financial_counts(s["engine"]) == before


@pytest.mark.parametrize(
    "split_amount,code",
    [
        ("24", "split_total_mismatch"),
        ("26", "split_total_mismatch"),
        ("0", "amount_positive"),
        ("-25", "amount_positive"),
    ],
)
def test_invalid_category_split_never_partially_posts(ledger_setup, split_amount, code):
    s = ledger_setup
    command = classified(s, splits=[{"category_id": s["expenses"][0].id, "amount": split_amount}])
    with pytest.raises(LedgerError) as error:
        s["posting"].post_expense(s["owner"], s["ledger"], command, "split")
    assert error.value.code == code
    assert financial_counts(s["engine"]) == (0, 0, 0, 0, 0)


def test_cross_ledger_and_wrong_kind_references_do_not_write(ledger_setup):
    s = ledger_setup
    other = s["structure"].create_entity(
        s["owner"],
        EntityCreate(
            kind="personal",
            name="Other fictional entity",
            base_asset_id="USD",
            template_key="personal_default",
        ),
    )
    foreign_category = s["structure"].list_categories(s["owner"], other.ledger.id)[0]
    for command, code in [
        (classified(s, account_id=uuid4()), "not_found"),
        (classified(s, splits=[{"category_id": foreign_category.id, "amount": "25"}]), "not_found"),
        (
            classified(s, splits=[{"category_id": s["income"].id, "amount": "25"}]),
            "category_kind_mismatch",
        ),
    ]:
        with pytest.raises(LedgerError) as error:
            s["posting"].post_expense(s["owner"], s["ledger"], command, "cross")
        assert error.value.code == code
    with pytest.raises(LedgerError) as error:
        s["posting"].post_opening(s["owner"], other.ledger.id, opening(s), "cross-account")
    assert error.value.code == "not_found"
    assert financial_counts(s["engine"]) == (0, 0, 0, 0, 0)
    assert s["posting"].balances(s["owner"], other.ledger.id) == []


@pytest.mark.parametrize(
    "inactive,code",
    [
        ("category", "category_archived"),
        ("account", "account_archived"),
        ("asset", "asset_disabled"),
        ("link", "account_asset_disabled"),
        ("entity", "entity_archived"),
    ],
)
def test_inactive_targets_deny_new_posting(ledger_setup, inactive, code):
    s = ledger_setup
    if inactive == "category":
        s["structure"].update_category(
            s["owner"],
            s["ledger"],
            s["expenses"][0].id,
            CategoryUpdate(expected_version=1, archived=True),
        )
    elif inactive == "account":
        s["structure"].update_account(
            s["owner"],
            s["ledger"],
            s["account"].id,
            AccountUpdate(expected_version=1, archived=True),
        )
    elif inactive == "asset":
        s["structure"].update_asset(
            s["owner"], "USD", AssetUpdate(expected_version=1, enabled=False)
        )
    elif inactive == "link":
        s["structure"].update_account(
            s["owner"],
            s["ledger"],
            s["account"].id,
            AccountUpdate(expected_version=1, asset_ids=["EUR"]),
        )
    else:
        s["structure"].update_entity(
            s["owner"], s["entity"].id, EntityUpdate(expected_version=1, archived=True)
        )
    with pytest.raises(LedgerError) as error:
        s["posting"].post_expense(s["owner"], s["ledger"], classified(s), "inactive")
    assert error.value.code == code
    assert financial_counts(s["engine"]) == (0, 0, 0, 0, 0)


def test_original_receipt_replays_after_archive_disable_and_service_restart(ledger_setup):
    s = ledger_setup
    request = classified(s)
    original = s["posting"].post_expense(s["owner"], s["ledger"], request, "persistent")
    s["structure"].update_category(
        s["owner"],
        s["ledger"],
        s["expenses"][0].id,
        CategoryUpdate(expected_version=1, archived=True),
    )
    s["structure"].update_account(
        s["owner"],
        s["ledger"],
        s["account"].id,
        AccountUpdate(expected_version=1, asset_ids=["EUR"], archived=True),
    )
    s["structure"].update_asset(s["owner"], "USD", AssetUpdate(expected_version=1, enabled=False))
    s["structure"].update_entity(
        s["owner"], s["entity"].id, EntityUpdate(expected_version=1, archived=True)
    )
    before = financial_counts(s["engine"])
    fresh = PostingService(s["engine"])
    assert fresh.post_expense(s["owner"], s["ledger"], request, "persistent") == original
    assert fresh.get_operation(s["owner"], s["ledger"], original.id).latest_posting == original
    balance = next(
        item for item in fresh.balances(s["owner"], s["ledger"]) if item.asset_id == "USD"
    )
    assert balance.amount == "-25.00" and balance.account_archived
    assert not balance.asset_enabled and not balance.link_enabled
    assert financial_counts(s["engine"]) == before
    with pytest.raises(LedgerError) as error:
        fresh.post_expense(
            s["owner"],
            s["ledger"],
            request.model_copy(update={"description": "Changed"}),
            "persistent",
        )
    assert error.value.code == "idempotency_conflict"
    with pytest.raises(LedgerError) as owner_error:
        fresh.post_expense(uuid4(), s["ledger"], request, "persistent")
    assert owner_error.value.code == "not_found"


def test_equivalent_quantity_text_is_a_different_idempotent_request(ledger_setup):
    s = ledger_setup
    request = opening(s, "1.00")
    original = s["posting"].post_opening(s["owner"], s["ledger"], request, "exact-wire")
    for changed in [
        request.model_copy(update={"amount": "1.0"}),
        request.model_copy(update={"id": original.id}),
    ]:
        with pytest.raises(LedgerError) as error:
            s["posting"].post_opening(s["owner"], s["ledger"], changed, "exact-wire")
        assert error.value.code == "idempotency_conflict"
    assert financial_counts(s["engine"]) == (1, 1, 2, 1, 1)


def test_opening_once_per_account_asset_and_signed_credit_card(ledger_setup):
    s = ledger_setup
    account = s["structure"].create_account(
        s["owner"],
        s["ledger"],
        AccountCreate(
            name="Fictional card",
            kind="credit_card",
            asset_ids=["USD", "EUR"],
        ),
    )
    request = opening(s, "-100", account_id=account.id)
    original = s["posting"].post_opening(s["owner"], s["ledger"], request, "card-opening")
    assert original.amount == "-100.00"
    with pytest.raises(LedgerError) as error:
        s["posting"].post_opening(s["owner"], s["ledger"], request, "second-opening")
    assert error.value.code == "opening_exists"
    s["posting"].post_opening(
        s["owner"], s["ledger"], opening(s, "20", "EUR", account_id=account.id), "euro-opening"
    )
    assert {
        item.asset_id: item.amount
        for item in s["posting"].balances(s["owner"], s["ledger"], account.id)
    } == {"USD": "-100.00", "EUR": "20.00"}


def test_balance_overflow_rolls_back_and_maximum_eth_remains_exact(ledger_setup):
    s = ledger_setup
    maximum = "99999999999999999999.999999999999999999"
    s["posting"].post_opening(s["owner"], s["ledger"], opening(s, maximum, "ETH"), "maximum")
    before = financial_counts(s["engine"])
    with pytest.raises(LedgerError) as error:
        s["posting"].post_income(
            s["owner"],
            s["ledger"],
            classified(s, "income", "0.000000000000000001", "ETH"),
            "overflow",
        )
    assert error.value.code == "amount_range"
    assert financial_counts(s["engine"]) == before and quantities(s)["ETH"] == maximum


def test_concurrent_same_key_creates_only_one_operation(ledger_setup):
    s = ledger_setup
    request = classified(s)

    def post(_):
        return PostingService(s["engine"]).post_expense(
            s["owner"], s["ledger"], request, "concurrent"
        )

    with ThreadPoolExecutor(max_workers=4) as pool:
        receipts = list(pool.map(post, range(4)))
    assert all(receipt == receipts[0] for receipt in receipts)
    assert financial_counts(s["engine"]) == (1, 1, 2, 0, 1)
    assert quantities(s)["USD"] == "-25.00"


def test_concurrent_changed_payload_conflicts_without_double_posting(ledger_setup):
    s = ledger_setup

    def post(amount):
        try:
            return (
                PostingService(s["engine"])
                .post_income(s["owner"], s["ledger"], classified(s, "income", amount), "contended")
                .amount
            )
        except LedgerError as error:
            return error.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(post, ["10", "20"]))
    assert results.count("idempotency_conflict") == 1
    assert quantities(s)["USD"] in {"10.00", "20.00"}
    assert financial_counts(s["engine"]) == (1, 1, 2, 0, 1)


def test_duplicate_client_operation_id_rolls_back_new_journal_and_receipt(ledger_setup):
    s = ledger_setup
    identifier = uuid4()
    request = classified(s, id=identifier)
    original = s["posting"].post_expense(s["owner"], s["ledger"], request, "client-id")
    assert original.id == identifier and original.journal_id != identifier
    before = financial_counts(s["engine"])
    with pytest.raises(LedgerError) as error:
        s["posting"].post_expense(s["owner"], s["ledger"], request, "different-key")
    assert error.value.code == "duplicate_record"
    assert financial_counts(s["engine"]) == before


def test_operation_reads_are_owner_ledger_scoped_and_pagination_is_stable(ledger_setup):
    s = ledger_setup
    receipts = [
        s["posting"].post_expense(s["owner"], s["ledger"], classified(s), f"expense-{index}")
        for index in range(3)
    ]
    listed = s["posting"].list_operations(s["owner"], s["ledger"])
    assert [item.id for item in listed] == [item.id for item in reversed(receipts)]
    assert [item.latest_posting for item in listed] == list(reversed(receipts))
    assert all(item.status == "active" and item.version == 1 for item in listed)
    assert s["posting"].list_operations(s["owner"], s["ledger"], limit=1, offset=1) == [listed[1]]
    assert s["posting"].list_operations(s["owner"], s["ledger"], offset=3) == []
    other = s["structure"].create_entity(
        s["owner"], EntityCreate(kind="personal", name="Other", base_asset_id="USD")
    )
    for operation in [
        lambda: s["posting"].get_operation(s["owner"], other.ledger.id, receipts[0].id),
        lambda: s["posting"].get_operation(uuid4(), s["ledger"], receipts[0].id),
        lambda: s["posting"].balances(s["owner"], other.ledger.id, s["account"].id),
    ]:
        with pytest.raises(LedgerError) as error:
            operation()
        assert error.value.code == "not_found"
    balances = s["posting"].balances(s["owner"], s["ledger"])
    assert s["posting"].balances(s["owner"], s["ledger"], limit=2, offset=1) == balances[1:3]
