"""Auditable revisions are atomic, exact, owner-scoped and replayable."""

from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
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
from coinpup_api.ledger.posting_schemas import (
    CancellationCreate,
    CorrectionCreate,
    ExchangeCreate,
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
def revision_setup(structure_database):
    engine, owner = structure_database
    structure, posting = LedgerService(engine), PostingService(engine)
    entity = structure.create_entity(
        owner,
        EntityCreate(
            kind="personal",
            name="Fictional revision owner",
            base_asset_id="USD",
            template_key="personal_default",
        ),
    )
    accounts = [
        structure.create_account(
            owner,
            entity.ledger.id,
            AccountCreate(
                name=f"Fictional account {index}",
                kind="bank",
                asset_ids=["USD", "EUR", "BTC", "ETH"],
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
        "expenses": [item for item in categories if item.kind == "expense"],
        "income": next(item for item in categories if item.kind == "income"),
    }


def opening(s, amount="1000", asset="USD", **changes):
    data = {
        "account_id": s["account"].id,
        "asset_id": asset,
        "amount": amount,
        "transaction_date": "2026-01-01",
    }
    data.update(changes)
    request = OpeningCreate(**data)
    receipt = s["posting"].post_opening(
        s["owner"], s["ledger"], request, f"opening-{request.account_id}-{asset}"
    )
    return request, receipt


def expense(s, amount="100", asset="USD", **changes):
    data = {
        "account_id": s["account"].id,
        "asset_id": asset,
        "amount": amount,
        "transaction_date": "2026-02-01",
        "recognition_date": "2026-01-31",
        "description": "Fictional expense",
        "splits": [{"category_id": s["expenses"][0].id, "amount": amount}],
    }
    data.update(changes)
    return ExpenseCreate(**data)


def correction(payload, kind, expected_version=1, reason="Correct fictional record", **changes):
    body = {**payload.model_dump(exclude={"id"}), "kind": kind, **changes}
    return CorrectionCreate(expected_version=expected_version, reason=reason, replacement=body)


def revise(s, receipt, command, key="correct"):
    return s["posting"].correct_operation(s["owner"], s["ledger"], receipt.id, command, key)


def cancel(s, receipt, version=1, key="cancel"):
    return s["posting"].cancel_operation(
        s["owner"],
        s["ledger"],
        receipt.id,
        CancellationCreate(expected_version=version, reason="Cancel fictional record"),
        key,
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


def test_expense_100_corrected_to_120_then_cancelled_preserves_audit_chain(revision_setup):
    s = revision_setup
    opening(s)
    request = expense(s)
    original = s["posting"].post_expense(s["owner"], s["ledger"], request, "original")
    fixed = revise(
        s,
        original,
        correction(
            request,
            "expense",
            amount="120",
            splits=[{"category_id": s["expenses"][0].id, "amount": "120"}],
            transaction_date="2026-03-01",
        ),
    )
    assert fixed.status == "active" and fixed.version == fixed.latest_posting.version == 2
    assert fixed.latest_posting.amount == "120.00"
    assert balances(s)[s["account"].id, "USD"] == "880.00"
    with Session(s["engine"]) as session:
        assert session.scalar(
            select(func.sum(JournalLine.amount)).where(JournalLine.role == "expense")
        ) == Decimal("120")
    cancelled = cancel(s, original, 2)
    assert cancelled.version == 3 and cancelled.status == "cancelled"
    assert (
        cancelled.latest_posting == fixed.latest_posting and cancelled.latest_posting.version == 2
    )
    assert cancelled.cancellation.version == 3
    assert balances(s)[s["account"].id, "USD"] == "1000.00"
    assert s["posting"].get_operation(s["owner"], s["ledger"], original.id) == cancelled
    history = s["posting"].history(s["owner"], s["ledger"], original.id)
    assert [(item.version, item.action) for item in history] == [
        (1, "create"),
        (2, "correct"),
        (3, "cancel"),
    ]
    assert history[0].reason is None and history[2].reason == "Cancel fictional record"
    assert [journal.kind for journal in history[1].journals] == ["reversal", "posting"]
    assert history[1].journals[0].transaction_date == original.transaction_date
    assert history[1].journals[1].transaction_date == fixed.latest_posting.transaction_date
    assert all(item.actor_id == s["owner"] for item in history)
    for source, reversal in [
        (history[0].journals[0], history[1].journals[0]),
        (history[1].journals[1], history[2].journals[0]),
    ]:
        assert reversal.reverses_journal_id == source.id
        for old, new in zip(source.lines, reversal.lines, strict=True):
            assert Decimal(new.amount) == Decimal(old.amount).copy_negate()
            assert (
                new.line_no,
                new.component_no,
                new.role,
                new.asset_id,
                new.account_id,
                new.category_id,
            ) == (
                old.line_no,
                old.component_no,
                old.role,
                old.asset_id,
                old.account_id,
                old.category_id,
            )
    assert s["posting"].history(s["owner"], s["ledger"], original.id, limit=1, offset=1) == [
        history[1]
    ]
    assert original.id in {
        item.id for item in s["posting"].list_operations(s["owner"], s["ledger"])
    }
    assert original.id not in {
        item.id for item in s["posting"].list_operations(s["owner"], s["ledger"], status="active")
    }
    assert s["posting"].list_operations(s["owner"], s["ledger"], status="cancelled") == [cancelled]


def test_original_and_revision_receipts_replay_original_versions_after_later_changes(
    revision_setup,
):
    s = revision_setup
    request = expense(s)
    original = s["posting"].post_expense(s["owner"], s["ledger"], request, "create-key")
    first_command = correction(
        request,
        "expense",
        amount="120",
        splits=[{"category_id": s["expenses"][0].id, "amount": "120"}],
    )
    first = revise(s, original, first_command, "first-key")
    second = revise(
        s,
        original,
        correction(
            request,
            "expense",
            expected_version=2,
            amount="130",
            splits=[{"category_id": s["expenses"][0].id, "amount": "130"}],
        ),
        "second-key",
    )
    assert second.version == 3
    before = counts(s)
    assert revise(s, original, first_command, "first-key") == first
    assert s["posting"].post_expense(s["owner"], s["ledger"], request, "create-key") == original
    assert counts(s) == before
    with pytest.raises(LedgerError) as error:
        revise(s, original, first_command.model_copy(update={"reason": "Changed"}), "first-key")
    assert error.value.code == "idempotency_conflict"


def test_cancelled_operation_is_terminal_and_opening_marker_is_retained(revision_setup):
    s = revision_setup
    request, original = opening(s)
    corrected = revise(s, original, correction(request, "opening", amount="1100"))
    assert balances(s)[s["account"].id, "USD"] == "1100.00"
    result = cancel(s, original, 2)
    assert result.latest_posting == corrected.latest_posting
    assert balances(s)[s["account"].id, "USD"] == "0.00"
    assert cancel(s, original, 2) == result
    for action in [
        lambda: cancel(s, original, 3, "again"),
        lambda: revise(
            s, original, correction(request, "opening", expected_version=3), "after-cancel"
        ),
    ]:
        with pytest.raises(LedgerError) as error:
            action()
        assert error.value.code == "operation_cancelled"
    with Session(s["engine"]) as session:
        assert session.get(OpeningPosition, (s["account"].id, "USD")).operation_id == original.id
    with pytest.raises(LedgerError) as error:
        s["posting"].post_opening(s["owner"], s["ledger"], request, "new-opening")
    assert error.value.code == "opening_exists"


@pytest.mark.parametrize("change", [{"asset_id": "EUR"}, {"account_id": "other"}])
def test_opening_correction_cannot_reassign_account_or_asset(revision_setup, change):
    s = revision_setup
    request, original = opening(s)
    values = {key: s["other"].id if value == "other" else value for key, value in change.items()}
    before = counts(s)
    with pytest.raises(LedgerError) as error:
        revise(s, original, correction(request, "opening", **values))
    assert error.value.code == "opening_identity" and counts(s) == before


@pytest.mark.parametrize("kind", ["income", "transfer", "exchange"])
def test_correction_and_cancellation_reverse_fees_for_all_financial_kinds(revision_setup, kind):
    s = revision_setup
    opening(s)
    fees = [
        {
            "account_id": s["account"].id,
            "asset_id": "USD",
            "amount": "2",
            "category_id": s["expenses"][0].id,
        }
    ]
    common = {"transaction_date": "2026-02-01", "fees": fees}
    if kind == "income":
        request = IncomeCreate(
            account_id=s["account"].id,
            asset_id="USD",
            amount="100",
            recognition_date="2026-02-01",
            splits=[{"category_id": s["income"].id, "amount": "100"}],
            **common,
        )
        changes = {"amount": "200", "splits": [{"category_id": s["income"].id, "amount": "200"}]}
        expected = "1197.00"
    elif kind == "transfer":
        request = TransferCreate(
            source_account_id=s["account"].id,
            destination_account_id=s["other"].id,
            asset_id="USD",
            amount="100",
            **common,
        )
        changes, expected = {"amount": "200"}, "797.00"
    else:
        request = ExchangeCreate(
            source_account_id=s["account"].id,
            source_asset_id="USD",
            source_amount="100",
            destination_account_id=s["other"].id,
            destination_asset_id="EUR",
            destination_amount="90",
            **common,
        )
        changes, expected = {"source_amount": "200", "destination_amount": "175"}, "797.00"
    original = getattr(s["posting"], "post_" + kind)(s["owner"], s["ledger"], request, "original")
    revised = revise(
        s, original, correction(request, kind, fees=[{**fees[0], "amount": "3"}], **changes)
    )
    assert revised.latest_posting.fees[0].amount == "3.00"
    assert balances(s)[s["account"].id, "USD"] == expected
    cancel(s, original, 2)
    current = balances(s)
    assert current[s["account"].id, "USD"] == "1000.00"
    assert current[s["other"].id, "USD"] == current[s["other"].id, "EUR"] == "0.00"
    history = s["posting"].history(s["owner"], s["ledger"], original.id)
    reversed_fee = [line for line in history[1].journals[0].lines if line.component_no == 1]
    assert {line.role: line.amount for line in reversed_fee} == {
        "account": "2.00",
        "expense": "-2.00",
    }


def test_old_inactive_references_can_be_reversed_into_active_new_references(revision_setup):
    s = revision_setup
    opening(s)
    request = expense(
        s,
        fees=[
            {
                "account_id": s["account"].id,
                "asset_id": "USD",
                "amount": "2",
                "category_id": s["expenses"][0].id,
            }
        ],
    )
    original = s["posting"].post_expense(s["owner"], s["ledger"], request, "old")
    s["structure"].update_account(
        s["owner"],
        s["ledger"],
        s["account"].id,
        AccountUpdate(expected_version=1, archived=True, asset_ids=["ETH"]),
    )
    s["structure"].update_asset(s["owner"], "USD", AssetUpdate(expected_version=1, enabled=False))
    s["structure"].update_category(
        s["owner"],
        s["ledger"],
        s["expenses"][0].id,
        CategoryUpdate(expected_version=1, archived=True),
    )
    changed = revise(
        s,
        original,
        correction(
            request,
            "expense",
            account_id=s["other"].id,
            asset_id="EUR",
            amount="90",
            splits=[{"category_id": s["expenses"][1].id, "amount": "90"}],
            fees=[],
        ),
    )
    assert (
        changed.latest_posting.asset_id == "EUR"
        and changed.latest_posting.account_id == s["other"].id
    )
    current = balances(s)
    assert (
        current[s["account"].id, "USD"] == "1000.00" and current[s["other"].id, "EUR"] == "-90.00"
    )
    cancel(s, original, 2)
    assert balances(s)[s["other"].id, "EUR"] == "0.00"


def test_cancellation_reverses_disabled_archived_old_references_but_entity_must_be_active(
    revision_setup,
):
    s = revision_setup
    request = expense(s)
    original = s["posting"].post_expense(s["owner"], s["ledger"], request, "original")
    s["structure"].update_account(
        s["owner"],
        s["ledger"],
        s["account"].id,
        AccountUpdate(expected_version=1, archived=True, asset_ids=["ETH"]),
    )
    s["structure"].update_asset(s["owner"], "USD", AssetUpdate(expected_version=1, enabled=False))
    s["structure"].update_category(
        s["owner"],
        s["ledger"],
        s["expenses"][0].id,
        CategoryUpdate(expected_version=1, archived=True),
    )
    result = cancel(s, original)
    assert result.status == "cancelled" and balances(s)[s["account"].id, "USD"] == "0.00"
    s["structure"].update_entity(
        s["owner"], s["entity"].id, EntityUpdate(expected_version=1, archived=True)
    )
    assert cancel(s, original) == result
    with pytest.raises(LedgerError) as error:
        cancel(s, original, 2, "new-key")
    assert error.value.code == "entity_archived"


def test_archived_entity_rejects_new_cancellation_without_changing_active_operation(revision_setup):
    s = revision_setup
    original = s["posting"].post_expense(s["owner"], s["ledger"], expense(s), "original")
    s["structure"].update_entity(
        s["owner"], s["entity"].id, EntityUpdate(expected_version=1, archived=True)
    )
    before = counts(s)
    with pytest.raises(LedgerError) as error:
        cancel(s, original)
    assert error.value.code == "entity_archived" and counts(s) == before
    assert s["posting"].get_operation(s["owner"], s["ledger"], original.id).status == "active"


def test_invalid_replacement_rolls_back_reversal_and_preserves_prior_version(revision_setup):
    s = revision_setup
    request = expense(s)
    original = s["posting"].post_expense(s["owner"], s["ledger"], request, "original")
    before, old = counts(s), balances(s)
    commands = [
        correction(request, "expense", amount="120"),
        correction(
            request,
            "expense",
            fees=[
                {
                    "account_id": uuid4(),
                    "asset_id": "USD",
                    "amount": "1",
                    "category_id": s["expenses"][0].id,
                }
            ],
        ),
        correction(request, "income", splits=[{"category_id": s["income"].id, "amount": "100"}]),
    ]
    for command in commands:
        with pytest.raises(LedgerError):
            revise(s, original, command)
        assert counts(s) == before and balances(s) == old
        assert s["posting"].get_operation(s["owner"], s["ledger"], original.id).version == 1


def test_final_net_balance_avoids_transient_overflow_but_cancel_overflow_is_rejected(
    revision_setup,
):
    s = revision_setup
    opening(s, "99999999999999999999.99")
    request = expense(s, "1")
    original = s["posting"].post_expense(s["owner"], s["ledger"], request, "expense")
    s["posting"].post_income(
        s["owner"],
        s["ledger"],
        IncomeCreate(
            account_id=s["account"].id,
            asset_id="USD",
            amount="1",
            transaction_date="2026-02-01",
            recognition_date="2026-02-01",
            splits=[{"category_id": s["income"].id, "amount": "1"}],
        ),
        "income",
    )
    before = counts(s)
    with pytest.raises(LedgerError) as error:
        cancel(s, original)
    assert error.value.code == "amount_range" and counts(s) == before
    fixed = revise(
        s,
        original,
        correction(
            request,
            "expense",
            amount="2",
            splits=[{"category_id": s["expenses"][0].id, "amount": "2"}],
        ),
    )
    assert fixed.version == 2 and balances(s)[s["account"].id, "USD"] == "99999999999999999998.99"


def test_maximum_eth_opening_cancellation_is_exact(revision_setup):
    s = revision_setup
    _, original = opening(s, "99999999999999999999.999999999999999999", "ETH")
    cancel(s, original)
    assert balances(s)[s["account"].id, "ETH"] == "0.000000000000000000"
    history = s["posting"].history(s["owner"], s["ledger"], original.id)
    assert history[1].journals[0].lines[0].amount == "-99999999999999999999.999999999999999999"


def test_concurrent_identical_corrections_commit_one_reversal_and_replacement(revision_setup):
    s = revision_setup
    request = expense(s)
    original = s["posting"].post_expense(s["owner"], s["ledger"], request, "original")
    command = correction(
        request,
        "expense",
        amount="120",
        splits=[{"category_id": s["expenses"][0].id, "amount": "120"}],
    )

    def execute(_):
        return PostingService(s["engine"]).correct_operation(
            s["owner"], s["ledger"], original.id, command, "same-revision"
        )

    with ThreadPoolExecutor(max_workers=3) as pool:
        states = list(pool.map(execute, range(3)))
    assert all(item == states[0] for item in states)
    assert counts(s) == (1, 3, 6, 2) and balances(s)[s["account"].id, "USD"] == "-120.00"


def test_concurrent_distinct_versioned_commands_have_one_winner(revision_setup):
    s = revision_setup
    request = expense(s)
    original = s["posting"].post_expense(s["owner"], s["ledger"], request, "original")

    def execute(amount):
        try:
            command = correction(
                request,
                "expense",
                amount=amount,
                splits=[{"category_id": s["expenses"][0].id, "amount": amount}],
            )
            return (
                PostingService(s["engine"])
                .correct_operation(
                    s["owner"], s["ledger"], original.id, command, f"revision-{amount}"
                )
                .latest_posting.amount
            )
        except LedgerError as error:
            return error.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(execute, ["120", "130"]))
    assert results.count("version_conflict") == 1 and counts(s) == (1, 3, 6, 2)


def test_history_and_revision_are_scoped_and_idempotency_includes_operation(revision_setup):
    s = revision_setup
    original = s["posting"].post_expense(s["owner"], s["ledger"], expense(s), "original")
    cancelled = cancel(s, original)
    other = s["structure"].create_entity(
        s["owner"], EntityCreate(kind="personal", name="Foreign", base_asset_id="USD")
    )
    for read in [
        lambda: s["posting"].history(s["owner"], other.ledger.id, original.id),
        lambda: s["posting"].history(uuid4(), s["ledger"], original.id),
    ]:
        with pytest.raises(LedgerError) as error:
            read()
        assert error.value.code == "not_found"
    with pytest.raises(LedgerError) as error:
        s["posting"].cancel_operation(
            s["owner"],
            s["ledger"],
            uuid4(),
            CancellationCreate(expected_version=1, reason="Cancel fictional record"),
            "cancel",
        )
    assert error.value.code == "idempotency_conflict"
    assert s["posting"].get_operation(s["owner"], s["ledger"], original.id) == cancelled
