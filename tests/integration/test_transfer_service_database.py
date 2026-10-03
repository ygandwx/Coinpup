"""Same-asset transfers and credit repayment preserve financial totals atomically."""

from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from uuid import uuid4

import pytest
from coinpup_api.ledger.models import CommandReceipt, FinancialOperation, Journal, JournalLine
from coinpup_api.ledger.posting import PostingService
from coinpup_api.ledger.posting_schemas import (
    ExpenseCreate,
    OpeningCreate,
    OperationResponse,
    TransferCreate,
    TransferResponse,
)
from coinpup_api.ledger.schemas import (
    AccountCreate,
    AccountUpdate,
    AssetUpdate,
    EntityCreate,
    EntityUpdate,
)
from coinpup_api.ledger.service import LedgerError, LedgerService
from sqlalchemy import func, select
from sqlalchemy.orm import Session

pytestmark = pytest.mark.integration


@pytest.fixture
def transfer_setup(structure_database):
    engine, owner = structure_database
    structure, posting = LedgerService(engine), PostingService(engine)
    entity = structure.create_entity(
        owner,
        EntityCreate(
            kind="personal",
            name="Fictional transfer owner",
            base_asset_id="CNY",
            template_key="personal_default",
        ),
    )
    accounts = {
        kind: structure.create_account(
            owner,
            entity.ledger.id,
            AccountCreate(name=f"Fictional {kind}", kind=kind, asset_ids=["CNY", "ETH"]),
        )
        for kind in ("bank", "cash", "credit_card")
    }
    category = next(
        item
        for item in structure.list_categories(owner, entity.ledger.id)
        if item.kind == "expense"
    )
    return {
        "engine": engine,
        "owner": owner,
        "structure": structure,
        "posting": posting,
        "entity": entity,
        "ledger": entity.ledger.id,
        "accounts": accounts,
        "category": category,
    }


def command(s, amount="200", source="bank", destination="cash", **changes):
    data = {
        "source_account_id": s["accounts"][source].id,
        "destination_account_id": s["accounts"][destination].id,
        "asset_id": "CNY",
        "amount": amount,
        "transaction_date": "2026-03-01",
        "description": "Fictional transfer",
    }
    data.update(changes)
    return TransferCreate(**data)


def initialize(s, account="bank", amount="1000", asset="CNY"):
    return s["posting"].post_opening(
        s["owner"],
        s["ledger"],
        OpeningCreate(
            account_id=s["accounts"][account].id,
            asset_id=asset,
            amount=amount,
            transaction_date="2026-01-01",
        ),
        f"opening-{account}-{asset}",
    )


def balance(s, account, asset="CNY"):
    return next(
        item.amount
        for item in s["posting"].balances(s["owner"], s["ledger"], s["accounts"][account].id)
        if item.asset_id == asset
    )


def counts(s):
    with Session(s["engine"]) as session:
        return tuple(
            session.scalar(select(func.count()).select_from(table))
            for table in (FinancialOperation, Journal, JournalLine, CommandReceipt)
        )


def test_a04_transfer_keeps_combined_balance_and_has_no_expense_income(transfer_setup):
    s = transfer_setup
    original_opening = initialize(s)
    request = command(s)
    receipt = s["posting"].post_transfer(s["owner"], s["ledger"], request, "a04")
    assert isinstance(receipt, TransferResponse)
    assert receipt.amount == "200.00" and receipt.recognition_date == receipt.transaction_date
    assert receipt.source_account_id == s["accounts"]["bank"].id
    assert receipt.destination_account_id == s["accounts"]["cash"].id
    assert balance(s, "bank") == "800.00" and balance(s, "cash") == "200.00"
    assert balance(s, "credit_card") == "0.00"
    assert s["posting"].post_transfer(s["owner"], s["ledger"], request, "a04") == receipt
    assert s["posting"].get_operation(s["owner"], s["ledger"], receipt.id) == receipt
    operations = s["posting"].list_operations(s["owner"], s["ledger"])
    assert operations == [receipt, original_opening]
    assert isinstance(operations[1], OperationResponse)
    assert "destination_account_id" not in operations[1].model_dump()
    assert counts(s) == (2, 2, 4, 2)
    with Session(s["engine"]) as session:
        assert (
            session.scalar(
                select(func.count())
                .select_from(JournalLine)
                .where(JournalLine.role.in_(["expense", "income"]))
            )
            == 0
        )
        assert session.get(Journal, receipt.journal_id).sealed
        legacy = session.get(CommandReceipt, (s["ledger"], "opening-bank-CNY"))
        assert legacy.response == original_opening.model_dump(mode="json")


def test_a06_card_expense_and_repayment_count_expense_only_once(transfer_setup):
    s = transfer_setup
    initialize(s)
    purchase = s["posting"].post_expense(
        s["owner"],
        s["ledger"],
        ExpenseCreate(
            account_id=s["accounts"]["credit_card"].id,
            asset_id="CNY",
            amount="100",
            transaction_date="2026-02-01",
            recognition_date="2026-02-01",
            splits=[{"category_id": s["category"].id, "amount": "100"}],
        ),
        "card-purchase",
    )
    assert balance(s, "credit_card") == "-100.00" and balance(s, "bank") == "1000.00"
    repayment = s["posting"].post_transfer(
        s["owner"], s["ledger"], command(s, "100", destination="credit_card"), "card-repayment"
    )
    assert balance(s, "bank") == "900.00" and balance(s, "credit_card") == "0.00"
    with Session(s["engine"]) as session:
        assert session.scalar(
            select(func.sum(JournalLine.amount)).where(JournalLine.role == "expense")
        ) == Decimal("100")
        assert (
            session.scalar(
                select(func.count())
                .select_from(JournalLine)
                .where(
                    JournalLine.journal_id == repayment.journal_id, JournalLine.role != "account"
                )
            )
            == 0
        )
    assert s["posting"].get_operation(s["owner"], s["ledger"], purchase.id) == purchase


@pytest.mark.parametrize(
    "amount,code",
    [
        ("0", "amount_positive"),
        ("-1", "amount_positive"),
        ("1.001", "amount_precision"),
        ("100000000000000000000", "amount_range"),
    ],
)
def test_invalid_transfer_quantity_leaves_both_accounts_unchanged(transfer_setup, amount, code):
    s = transfer_setup
    initialize(s)
    before = counts(s)
    with pytest.raises(LedgerError) as error:
        s["posting"].post_transfer(s["owner"], s["ledger"], command(s, amount), "invalid-quantity")
    assert error.value.code == code
    assert counts(s) == before and balance(s, "bank") == "1000.00" and balance(s, "cash") == "0.00"


@pytest.mark.parametrize("side", ["source", "destination"])
def test_either_balance_overflow_rolls_back_the_complete_transfer(transfer_setup, side):
    s = transfer_setup
    maximum = "99999999999999999999.999999999999999999"
    initialize(
        s,
        "bank" if side == "source" else "cash",
        "-" + maximum if side == "source" else maximum,
        "ETH",
    )
    before, original_bank, original_cash = (
        counts(s),
        balance(s, "bank", "ETH"),
        balance(s, "cash", "ETH"),
    )
    with pytest.raises(LedgerError) as error:
        s["posting"].post_transfer(
            s["owner"], s["ledger"], command(s, "0.000000000000000001", asset_id="ETH"), "overflow"
        )
    assert error.value.code == "amount_range"
    assert counts(s) == before
    assert balance(s, "bank", "ETH") == original_bank and balance(s, "cash", "ETH") == original_cash


def test_transfer_preserves_eighteen_fraction_digits(transfer_setup):
    s = transfer_setup
    initialize(s, "bank", "1.000000000000000001", "ETH")
    receipt = s["posting"].post_transfer(
        s["owner"], s["ledger"], command(s, "0.000000000000000001", asset_id="ETH"), "eth-transfer"
    )
    assert receipt.amount == "0.000000000000000001"
    assert balance(s, "bank", "ETH") == "1.000000000000000000"
    assert balance(s, "cash", "ETH") == "0.000000000000000001"


@pytest.mark.parametrize("side", ["source_account_id", "destination_account_id"])
def test_cross_ledger_transfer_accounts_are_rejected(transfer_setup, side):
    s = transfer_setup
    other = s["structure"].create_entity(
        s["owner"], EntityCreate(kind="personal", name="Other", base_asset_id="CNY")
    )
    foreign = s["structure"].create_account(
        s["owner"], other.ledger.id, AccountCreate(name="Foreign", kind="bank", asset_ids=["CNY"])
    )
    with pytest.raises(LedgerError) as error:
        s["posting"].post_transfer(
            s["owner"], s["ledger"], command(s, **{side: foreign.id}), "cross-ledger"
        )
    assert error.value.code == "not_found" and counts(s) == (0, 0, 0, 0)
    assert s["posting"].balances(s["owner"], other.ledger.id)[0].amount == "0.00"


@pytest.mark.parametrize(
    "side,state,code",
    [
        ("bank", "archive", "account_archived"),
        ("cash", "archive", "account_archived"),
        ("bank", "disable", "account_asset_disabled"),
        ("cash", "disable", "account_asset_disabled"),
    ],
)
def test_both_accounts_require_active_asset_links(transfer_setup, side, state, code):
    s = transfer_setup
    change = {"archived": True} if state == "archive" else {"asset_ids": ["ETH"]}
    s["structure"].update_account(
        s["owner"], s["ledger"], s["accounts"][side].id, AccountUpdate(expected_version=1, **change)
    )
    with pytest.raises(LedgerError) as error:
        s["posting"].post_transfer(s["owner"], s["ledger"], command(s), "inactive-side")
    assert error.value.code == code and counts(s) == (0, 0, 0, 0)


def test_transfer_replay_after_archiving_and_disabling_is_exact_and_readable(transfer_setup):
    s = transfer_setup
    initialize(s)
    request = command(s, id=uuid4())
    receipt = s["posting"].post_transfer(s["owner"], s["ledger"], request, "stable-transfer")
    for account in ("bank", "cash"):
        s["structure"].update_account(
            s["owner"],
            s["ledger"],
            s["accounts"][account].id,
            AccountUpdate(expected_version=1, archived=True, asset_ids=["ETH"]),
        )
    s["structure"].update_asset(s["owner"], "CNY", AssetUpdate(expected_version=1, enabled=False))
    s["structure"].update_entity(
        s["owner"], s["entity"].id, EntityUpdate(expected_version=1, archived=True)
    )
    fresh, before = PostingService(s["engine"]), counts(s)
    assert fresh.post_transfer(s["owner"], s["ledger"], request, "stable-transfer") == receipt
    assert fresh.get_operation(s["owner"], s["ledger"], receipt.id) == receipt
    assert counts(s) == before and balance(s, "bank") == "800.00" and balance(s, "cash") == "200.00"
    with pytest.raises(LedgerError) as error:
        fresh.post_transfer(
            s["owner"], s["ledger"], request.model_copy(update={"amount": "201"}), "stable-transfer"
        )
    assert error.value.code == "idempotency_conflict"
    with pytest.raises(LedgerError) as error:
        fresh.post_transfer(uuid4(), s["ledger"], request, "stable-transfer")
    assert error.value.code == "not_found"


def test_concurrent_opposite_transfers_serialize_without_deadlock_or_lost_amount(transfer_setup):
    s = transfer_setup
    initialize(s)
    initialize(s, "cash", "500")

    def post(arguments):
        amount, source, destination, key = arguments
        return PostingService(s["engine"]).post_transfer(
            s["owner"], s["ledger"], command(s, amount, source, destination), key
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        receipts = list(
            pool.map(post, [("100", "bank", "cash", "forward"), ("70", "cash", "bank", "backward")])
        )
    assert len({receipt.id for receipt in receipts}) == 2
    assert balance(s, "bank") == "970.00" and balance(s, "cash") == "530.00"
    assert counts(s) == (4, 4, 8, 4)


def test_concurrent_transfer_retries_create_one_two_sided_journal(transfer_setup):
    s = transfer_setup
    initialize(s)
    request = command(s)

    def post(_):
        return PostingService(s["engine"]).post_transfer(
            s["owner"], s["ledger"], request, "concurrent-repeat"
        )

    with ThreadPoolExecutor(max_workers=4) as pool:
        receipts = list(pool.map(post, range(4)))
    assert all(receipt == receipts[0] for receipt in receipts)
    assert counts(s) == (2, 2, 4, 2)
    assert balance(s, "bank") == "800.00" and balance(s, "cash") == "200.00"


def test_command_key_scope_includes_kind_and_client_id_conflict_is_atomic(transfer_setup):
    s = transfer_setup
    initialize(s)
    request = command(s, id=uuid4())
    with pytest.raises(LedgerError) as error:
        s["posting"].post_transfer(s["owner"], s["ledger"], request, "opening-bank-CNY")
    assert error.value.code == "idempotency_conflict"
    original = s["posting"].post_transfer(s["owner"], s["ledger"], request, "client-transfer")
    before = counts(s)
    with pytest.raises(LedgerError) as error:
        s["posting"].post_transfer(s["owner"], s["ledger"], request, "new-key-same-id")
    assert error.value.code == "duplicate_record"
    assert counts(s) == before and original.id == request.id
