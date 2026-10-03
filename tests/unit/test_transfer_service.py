"""Transfer command boundaries and backwards-compatible receipt decoding."""

import hashlib
import json
from datetime import UTC, date, datetime
from decimal import Decimal
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from coinpup_api.ledger.posting import PostingService, command_hash
from coinpup_api.ledger.posting_schemas import (
    FinancialResponse,
    OpeningCreate,
    OperationResponse,
    TransferCreate,
    TransferResponse,
)
from coinpup_api.ledger.service import LedgerError
from pydantic import TypeAdapter, ValidationError


def transfer(**changes):
    data = {
        "source_account_id": uuid4(),
        "destination_account_id": uuid4(),
        "asset_id": "USD",
        "amount": "200.00",
        "transaction_date": "2026-03-01",
        "description": "Fictional transfer",
    }
    data.update(changes)
    return TransferCreate(**data)


def test_distinct_transfer_accounts_and_explicit_date_contract():
    account = uuid4()
    with pytest.raises(ValidationError):
        transfer(source_account_id=account, destination_account_id=account)
    with pytest.raises(ValidationError):
        transfer(recognition_date="2026-01-01")
    with pytest.raises(ValidationError):
        transfer(transaction_date=1772323200)
    with pytest.raises(ValidationError):
        transfer(account_id=account)
    assert transfer().transaction_date == date(2026, 3, 1)


@pytest.mark.parametrize("amount", [True, 1.0, 1, "1e2", " 1", "1.00\n", "9" * 41])
def test_transfer_requires_strict_decimal_strings(amount):
    with pytest.raises(ValidationError):
        transfer(amount=amount)


def test_legacy_hash_payload_has_no_added_fields_after_shared_schema_refactor():
    # This literal describes the pre-transfer command shape, including every default.
    old = {
        "id": None,
        "account_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        "asset_id": "USD",
        "amount": "100.00",
        "transaction_date": "2026-01-01",
        "description": "",
    }
    ledger = UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
    encoded = json.dumps(
        {"kind": "opening", "ledger_id": str(ledger), "payload": old},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    command = OpeningCreate.model_validate(old)
    assert command.model_dump(mode="json") == old
    assert command_hash("opening", ledger, command) == hashlib.sha256(encoded.encode()).hexdigest()


def test_legacy_receipt_roundtrip_does_not_add_transfer_fields():
    original = {
        "id": str(uuid4()),
        "ledger_id": str(uuid4()),
        "journal_id": str(uuid4()),
        "kind": "opening",
        "version": 1,
        "account_id": str(uuid4()),
        "asset_id": "USD",
        "amount": "100.00",
        "transaction_date": "2026-01-01",
        "recognition_date": "2026-01-01",
        "description": "",
        "splits": [],
        "created_at": "2026-01-01T00:00:00Z",
    }
    restored = TypeAdapter(FinancialResponse).validate_python(original)
    assert isinstance(restored, OperationResponse)
    assert restored.model_dump(mode="json") == original
    assert "source_account_id" not in restored.model_dump()
    assert "destination_account_id" not in restored.model_dump()


def transfer_rows():
    operation = SimpleNamespace(
        id=uuid4(), ledger_id=uuid4(), version=1, created_at=datetime.now(UTC)
    )
    journal = SimpleNamespace(
        id=uuid4(),
        operation_version=1,
        created_at=operation.created_at,
        transaction_date=date(2026, 3, 1),
        recognition_date=date(2026, 3, 1),
        description="Test",
    )
    source = SimpleNamespace(
        account_id=uuid4(), asset_id="BTC", role="account", amount=Decimal("-0.12345678")
    )
    destination = SimpleNamespace(
        account_id=uuid4(), asset_id="BTC", role="account", amount=Decimal("0.12345678")
    )
    asset = SimpleNamespace(
        code="BTC", kind="native", scale=8, network="bitcoin", token_reference=None, enabled=False
    )
    session = SimpleNamespace(get=lambda _model, _identifier: asset)
    return session, operation, journal, source, destination


def test_transfer_reader_derives_accounts_by_sign_not_row_order():
    session, operation, journal, source, destination = transfer_rows()
    result = PostingService._read_transfer(session, operation, journal, [destination, source])
    assert isinstance(result, TransferResponse)
    assert result.source_account_id == source.account_id
    assert result.destination_account_id == destination.account_id
    assert result.amount == "0.12345678"
    assert "account_id" not in result.model_dump() and "splits" not in result.model_dump()
    assert TypeAdapter(FinancialResponse).validate_json(result.model_dump_json()) == result


@pytest.mark.parametrize(
    "corruption",
    [
        "same_account",
        "other_asset",
        "category",
        "unbalanced",
        "both_positive",
        "precision",
        "wrong_date",
    ],
)
def test_transfer_reader_rejects_corrupted_shapes(corruption):
    session, operation, journal, source, destination = transfer_rows()
    if corruption == "same_account":
        destination.account_id = source.account_id
    elif corruption == "other_asset":
        destination.asset_id = "ETH"
    elif corruption == "category":
        destination.role = "expense"
    elif corruption == "unbalanced":
        destination.amount = Decimal("0.12")
    elif corruption == "both_positive":
        source.amount = destination.amount
    elif corruption == "precision":
        destination.amount = Decimal("0.123456789")
    else:
        journal.recognition_date = date(2026, 1, 1)
    with pytest.raises(LedgerError) as error:
        PostingService._read_transfer(session, operation, journal, [source, destination])
    assert (error.value.code, error.value.status) == ("ledger_integrity", 503)


def test_transfer_hash_distinguishes_direction_and_kind():
    request = transfer()
    reverse = request.model_copy(
        update={
            "source_account_id": request.destination_account_id,
            "destination_account_id": request.source_account_id,
        }
    )
    ledger = uuid4()
    assert command_hash("transfer", ledger, request) != command_hash("transfer", ledger, reverse)
    assert command_hash("transfer", ledger, request) != command_hash("expense", ledger, request)
    assert command_hash("transfer", ledger, request) == command_hash(
        "transfer", ledger, TransferCreate.model_validate_json(request.model_dump_json())
    )


@pytest.mark.parametrize("key", ["", "space key", "汉字", "a" * 129])
def test_transfer_reuses_key_validation_before_storage(key):
    with pytest.raises(LedgerError) as error:
        PostingService(None).post_transfer(uuid4(), uuid4(), transfer(), key)
    assert error.value.code == "invalid_idempotency_key"
