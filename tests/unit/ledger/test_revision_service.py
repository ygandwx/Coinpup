"""Revision requests, state invariants and context-independent exact reversal rows."""

from datetime import UTC, datetime
from decimal import Decimal, Inexact, localcontext
from types import SimpleNamespace
from uuid import uuid4

import pytest
from coinpup_api.ledger.assets import get_asset
from coinpup_api.ledger.posting import PostingService
from coinpup_api.ledger.posting_schemas import (
    CancellationCreate,
    CorrectionCreate,
    OperationResponse,
    OperationState,
)
from coinpup_api.ledger.posting_storage import prepare_reversal_lines
from coinpup_api.ledger.revisions import revision_hash
from coinpup_api.ledger.service import LedgerError
from pydantic import ValidationError


def replacement(kind="expense", **changes):
    account, destination, category = uuid4(), uuid4(), uuid4()
    data = {"kind": kind, "transaction_date": "2026-01-02"}
    if kind == "exchange":
        data.update(
            source_account_id=account,
            source_asset_id="USD",
            source_amount="100",
            destination_account_id=destination,
            destination_asset_id="EUR",
            destination_amount="90",
        )
    else:
        data.update(asset_id="USD", amount="100")
        if kind == "transfer":
            data.update(source_account_id=account, destination_account_id=destination)
        else:
            data["account_id"] = account
        if kind in {"income", "expense"}:
            data.update(
                recognition_date="2026-01-01", splits=[{"category_id": category, "amount": "100"}]
            )
    data.update(changes)
    return data


@pytest.mark.parametrize("kind", ["opening", "income", "expense", "transfer", "exchange"])
def test_replacement_forbids_client_operation_id_even_explicit_null(kind):
    body = replacement(kind)
    parsed = CorrectionCreate(expected_version=1, reason="Correct fictional data", replacement=body)
    assert parsed.replacement.kind == kind and "id" not in parsed.replacement.model_dump()
    for identifier in (None, str(uuid4())):
        with pytest.raises(ValidationError):
            CorrectionCreate(
                expected_version=1, reason="Correct", replacement={**body, "id": identifier}
            )


@pytest.mark.parametrize("reason", ["", "   ", "x" * 1001, "bad\x00reason", 42])
def test_reason_is_required_bounded_and_never_coerced(reason):
    with pytest.raises(ValidationError):
        CancellationCreate(expected_version=1, reason=reason)


@pytest.mark.parametrize("version", [0, -1, True, "1", 1.0])
def test_revision_version_is_a_positive_strict_integer(version):
    with pytest.raises(ValidationError):
        CancellationCreate(expected_version=version, reason="Cancel fictional data")


def test_revision_hash_is_scoped_to_action_operation_version_and_full_body():
    ledger, operation = uuid4(), uuid4()
    command = CancellationCreate(expected_version=1, reason="  Cancel fictional data  ")
    assert command.reason == "Cancel fictional data"
    digest = revision_hash("cancel", ledger, operation, command)
    assert digest == revision_hash(
        "cancel",
        ledger,
        operation,
        CancellationCreate.model_validate_json(command.model_dump_json()),
    )
    assert digest != revision_hash("correct", ledger, operation, command)
    assert digest != revision_hash("cancel", uuid4(), operation, command)
    assert digest != revision_hash("cancel", ledger, uuid4(), command)
    assert digest != revision_hash(
        "cancel", ledger, operation, command.model_copy(update={"expected_version": 2})
    )
    assert digest != revision_hash(
        "cancel", ledger, operation, command.model_copy(update={"reason": "Different"})
    )
    corrected = CorrectionCreate(expected_version=1, reason="Correct", replacement=replacement())
    changed = corrected.model_copy(
        update={
            "replacement": corrected.replacement.model_copy(update={"description": "Different"})
        }
    )
    assert revision_hash("correct", ledger, operation, corrected) != revision_hash(
        "correct", ledger, operation, changed
    )


def original_receipt():
    return OperationResponse(
        id=uuid4(),
        ledger_id=uuid4(),
        journal_id=uuid4(),
        kind="expense",
        version=2,
        account_id=uuid4(),
        asset_id="USD",
        amount="120.00",
        transaction_date="2026-01-01",
        recognition_date="2026-01-01",
        description="",
        splits=[],
        created_at=datetime.now(UTC),
    )


def test_cancelled_state_version_is_newer_than_latest_posting_version():
    receipt = original_receipt()
    state = OperationState(
        id=receipt.id,
        ledger_id=receipt.ledger_id,
        kind=receipt.kind,
        version=3,
        status="cancelled",
        latest_posting=receipt,
        updated_at=datetime.now(UTC),
        cancellation={
            "version": 3,
            "reversal_journal_id": uuid4(),
            "reason": "Cancel",
            "recorded_at": datetime.now(UTC),
        },
    )
    assert state.latest_posting.version == 2 and state.version == 3
    assert "cancellation" in state.model_dump(mode="json")
    assert OperationState.model_validate_json(state.model_dump_json()) == state
    active = OperationState(
        id=receipt.id,
        ledger_id=receipt.ledger_id,
        kind=receipt.kind,
        version=2,
        status="active",
        latest_posting=receipt,
        updated_at=datetime.now(UTC),
    )
    assert "cancellation" not in active.model_dump(mode="json")
    for invalid in [
        {**active.model_dump(), "version": 3},
        {**active.model_dump(), "status": "cancelled"},
        {**active.model_dump(), "id": uuid4()},
        {**state.model_dump(), "status": "active"},
    ]:
        with pytest.raises(ValidationError):
            OperationState.model_validate(invalid)


def source_row(
    journal,
    ledger,
    position,
    role,
    amount,
    *,
    asset="ETH",
    component=0,
    account=None,
    category=None,
):
    return SimpleNamespace(
        id=uuid4(),
        journal_id=journal,
        ledger_id=ledger,
        line_no=position,
        component_no=component,
        role=role,
        asset_id=asset,
        amount=Decimal(amount),
        account_id=account,
        category_id=category,
        project_id=None,
        party_id=None,
        counterparty_entity_id=None,
        document_id=None,
        document_line_id=None,
        dimension_owner_id=None,
    )


def test_reversal_of_maximum_precision_never_rounds_under_hostile_decimal_context():
    journal, ledger, reversal, account = uuid4(), uuid4(), uuid4(), uuid4()
    maximum = "99999999999999999999.999999999999999999"
    original = [
        source_row(journal, ledger, 1, "account", maximum, account=account),
        source_row(journal, ledger, 2, "equity", "-" + maximum),
    ]
    with localcontext() as context:
        context.prec = 2
        context.traps[Inexact] = True
        reversed_lines, parameters = prepare_reversal_lines(
            original, reversal, {"ETH": get_asset("ETH")}
        )
    assert parameters[0]["amount"] == Decimal("-" + maximum)
    assert parameters[1]["amount"] == Decimal(maximum)
    for source, reversed_line in zip(original, reversed_lines, strict=True):
        assert source.id != reversed_line.id and reversed_line.journal_id == reversal
        assert (
            source.account_id == reversed_line.account_id
            and source.line_no == reversed_line.line_no
        )
        assert reversed_line.amount.to_decimal() == source.amount.copy_negate()


def test_exact_reversal_preserves_fee_components_and_reverses_fee_roles_signs():
    journal, ledger, account, category = uuid4(), uuid4(), uuid4(), uuid4()
    original = [
        source_row(journal, ledger, 1, "account", "-0.1", asset="BTC", account=account),
        source_row(journal, ledger, 2, "expense", "0.1", asset="BTC", category=category),
        source_row(
            journal, ledger, 3, "account", "-0.00001", asset="BTC", component=1, account=account
        ),
        source_row(
            journal, ledger, 4, "expense", "0.00001", asset="BTC", component=1, category=category
        ),
    ]
    lines, parameters = prepare_reversal_lines(original, uuid4(), {"BTC": get_asset("BTC")})
    assert [line.amount.to_string() for line in lines] == [
        "0.10000000",
        "-0.10000000",
        "0.00001000",
        "-0.00001000",
    ]
    for before, after in zip(original, parameters, strict=True):
        for field in (
            "line_no",
            "component_no",
            "role",
            "asset_id",
            "account_id",
            "category_id",
            "ledger_id",
        ):
            assert after[field] == getattr(before, field)


@pytest.mark.parametrize("key", ["", "bad key", "汉字", "x" * 129])
def test_revision_key_is_validated_before_connecting_to_storage(key):
    with pytest.raises(LedgerError) as error:
        PostingService(None).cancel_operation(
            uuid4(), uuid4(), uuid4(), CancellationCreate(expected_version=1, reason="Cancel"), key
        )
    assert error.value.code == "invalid_idempotency_key"
