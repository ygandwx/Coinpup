"""Fictional single-refund intents and fixed receipts, without a database or router."""

import json
from decimal import localcontext
from uuid import UUID

import pytest
from coinpup_api.business.refund_revision_hash import refund_revision_hash_v2
from coinpup_api.business.refund_revision_schemas import (
    RefundCancellationCreate,
    RefundCorrectionCreate,
    RefundReplacement,
    RefundRevisionResponse,
    validate_refund_revision_amounts,
)
from coinpup_api.business.refund_schemas import BusinessRefundCreate, BusinessRefundResponse
from coinpup_api.ledger.assets import ASSETS, AssetDefinition
from coinpup_api.ledger.errors import MoneyError
from coinpup_api.ledger.idempotency import _v2_hash
from coinpup_api.ledger.posting_schemas import FeeCreate
from coinpup_api.ledger.service import LedgerError
from pydantic import ValidationError


def uid(number):
    return str(UUID(int=number))


def replacement(**changes):
    return {
        "account_id": uid(1),
        "asset_id": "EUR",
        "amount": "11.00",
        "payment_amount": "12.00",
        "transaction_date": "2031-02-03",
        "reason": "Fictional correction",
    } | changes


def fee(**changes):
    return {
        "account_id": uid(2),
        "asset_id": "BTC",
        "amount": "0.00000001",
        "category_id": uid(3),
    } | changes


def request(action, **changes):
    data = {"expected_version": 3, "reason": "Fictional revision"}
    if action == "correct":
        data["replacement"] = replacement()
    return data | changes


def fact(**changes):
    return (
        replacement(
            payment_asset_id="USD",
            source_version=1,
            recognition_date="2031-02-03",
            fees=[fee()],
            allocation_return_ids=[uid(11), uid(12)],
        )
        | changes
    )


def response(action="correct", **changes):
    return {
        "kind": "business_refund_revision",
        "refund_id": uid(5),
        "revision_id": uid(8),
        "ledger_id": uid(7),
        "payment_id": uid(6),
        "operation_id": uid(9),
        "previous_version": 3,
        "version": 4,
        "action": action,
        "reason": "Fictional revision",
        "previous_journal_id": uid(21),
        "reversal_journal_id": uid(22),
        "replacement_journal_id": uid(23) if action == "correct" else None,
        "previous": fact(),
        "replacement": fact(
            account_id=uid(30),
            asset_id="USD",
            amount="13",
            payment_amount="13.000",
            transaction_date="2031-03-04",
            recognition_date="2031-03-04",
            reason="Fictional next fact",
            fees=[],
            allocation_return_ids=[uid(12)],
        )
        if action == "correct"
        else None,
    } | changes


@pytest.mark.parametrize(
    "model,action", [(RefundCorrectionCreate, "correct"), (RefundCancellationCreate, "cancel")]
)
def test_version_range_and_independent_revision_reason(model, action):
    assert (
        model.model_validate(request(action, expected_version=2147483646)).expected_version
        == 2147483646
    )
    for invalid in [False, True, 1.0, "1", 0, 2147483647]:
        with pytest.raises(ValidationError):
            model.model_validate(request(action, expected_version=invalid))
    reason = "🐾" * 1000
    assert model.model_validate(request(action, reason="\x85" + reason + "\x1f")).reason == reason
    for invalid in [" ", "a\0b", reason + "x"]:
        with pytest.raises(ValidationError):
            model.model_validate(request(action, reason=invalid))


def test_replacement_keeps_the_original_fact_reason_limit_and_cannot_rebind_payment():
    reason = "🐾" * 2000
    assert RefundReplacement.model_validate(replacement(reason=reason)).reason == reason
    for invalid in [" " + reason, "ab\0c", "\x85"]:
        with pytest.raises(ValidationError):
            RefundReplacement.model_validate(replacement(reason=invalid))
    assert RefundReplacement.model_validate(replacement(reason="\x85x\ufeff ")).reason == "x\ufeff"
    for field in [
        "id",
        "refund_id",
        "payment_id",
        "operation_id",
        "source_version",
        "payment_asset_id",
        "recognition_date",
    ]:
        with pytest.raises(ValidationError):
            RefundReplacement.model_validate(replacement(**{field: uid(10)}))
    with pytest.raises(ValidationError):
        RefundCorrectionCreate.model_validate(request("cancel"))
    with pytest.raises(ValidationError):
        RefundCancellationCreate.model_validate(request("cancel", replacement=None))


def test_complete_ordered_lists_and_empty_defaults():
    refs = [uid(i) for i in range(399, 199, -1)]
    fees = [fee(category_id=uid(31)), fee()] * 10
    parsed = RefundReplacement.model_validate(replacement(fees=fees, allocation_return_ids=refs))
    assert parsed.model_dump(mode="json")["fees"] == fees
    assert [str(identifier) for identifier in parsed.allocation_return_ids] == refs
    empty = RefundReplacement.model_validate(replacement())
    assert empty == RefundReplacement.model_validate(replacement(fees=[], allocation_return_ids=[]))
    assert empty.fees == empty.allocation_return_ids == []
    for invalid in [
        {"fees": None},
        {"fees": fees + [fee()]},
        {"allocation_return_ids": None},
        {"allocation_return_ids": refs + [uid(400)]},
        {"allocation_return_ids": [uid(1), uid(1)]},
    ]:
        with pytest.raises(ValidationError):
            RefundReplacement.model_validate(replacement(**invalid))


@pytest.mark.parametrize("value", ["0", "-0.00", "-1", ".01", "1e2", 1, "1" * 21, "0." + "1" * 19])
def test_all_new_quantities_must_be_positive_bounded_strings(value):
    for field in ["amount", "payment_amount", "fees"]:
        with pytest.raises(ValidationError):
            RefundReplacement.model_validate(
                replacement(**{field: [fee(amount=value)] if field == "fees" else value})
            )
    assert FeeCreate.model_validate(fee(amount="-1")).amount == "-1"


def test_fixed_versions_keep_complete_old_new_and_cancelled_facts():
    for action in ["correct", "cancel"]:
        data = response(action, previous_version=2147483646, version=2147483647)
        parsed = RefundRevisionResponse.model_validate(data)
        assert parsed.model_dump(mode="json") == data
        assert parsed.previous.source_version == 1
        assert isinstance(parsed.previous.fees, tuple)
        assert isinstance(parsed.previous.allocation_return_ids, tuple)
        with pytest.raises(ValidationError):
            parsed.previous.fees[0].amount = "3"


@pytest.mark.parametrize(
    "changes",
    [
        {"kind": "business_refund"},
        {"action": "amend"},
        {"previous_version": True},
        {"version": 4.0},
        {"version": 3},
        {"previous_version": 2147483647, "version": 2147483648},
        {"reason": "  saved"},
        {"reason": "saved\0"},
        {"reason": "a" * 1001},
        {"reversal_journal_id": uid(21)},
        {"replacement_journal_id": uid(22)},
        {"replacement_journal_id": uid(21)},
        {"replacement": None},
        {"replacement_journal_id": None},
        {"replacement": fact(payment_asset_id="HKD")},
        {"action": "cancel"},
        {"action": "cancel", "replacement": None},
        {"action": "cancel", "replacement_journal_id": None},
    ],
)
def test_inconsistent_revision_receipts_are_rejected(changes):
    with pytest.raises(ValidationError):
        RefundRevisionResponse.model_validate(response() | changes)


@pytest.mark.parametrize(
    "changes",
    [
        {"source_version": 2},
        {"source_version": True},
        {"source_version": 1.0},
        {"payment_id": uid(999)},
        {"recognition_date": "2031-02-04"},
        {"transaction_date": "2031-02-30"},
        {"reason": "\x85untrimmed"},
        {"reason": "a" * 2001},
        {"fees": None},
        {"fees": [fee()] * 21},
        {"fees": [fee(amount="0")]},
        {"fees": [fee(amount="0." + "1" * 19)]},
        {"allocation_return_ids": [uid(11), uid(11)]},
        {"amount": "0"},
        {"payment_amount": "-0.00"},
    ],
)
def test_invalid_prior_facts_cannot_be_hidden_by_cancellation(changes):
    with pytest.raises(ValidationError):
        RefundRevisionResponse.model_validate(response("cancel", previous=fact(**changes)))


@pytest.mark.parametrize(
    "missing", ["fees", "allocation_return_ids", "reason", "recognition_date", "source_version"]
)
def test_frozen_facts_require_complete_fields_even_for_empty_collections(missing):
    old = fact(fees=[], allocation_return_ids=[])
    del old[missing]
    with pytest.raises(ValidationError):
        RefundRevisionResponse.model_validate(response("cancel", previous=old))


def test_actual_asset_validation_preserves_old_disabled_assets_and_padded_spelling():
    token = AssetDefinition("USDT", "token", 6, "fictional-chain", "FictionalToken", enabled=False)
    disabled_btc = AssetDefinition("BTC", "native", 8, "bitcoin", enabled=False)
    catalog = dict(ASSETS) | {token.asset_id: token, "BTC": disabled_btc}
    data = response(
        previous=fact(
            asset_id=token.asset_id,
            amount="12.123456000000000000",
            payment_amount="12.000000000000000000",
            fees=[fee(amount="0.000000010000000000")],
        ),
        replacement=fact(
            asset_id="ETH",
            amount="9" * 20 + "." + "9" * 18,
            payment_amount="0.010000000000000000",
            fees=[fee(asset_id="ETH", amount="0." + "0" * 17 + "1")],
        ),
    )
    parsed = RefundRevisionResponse.model_validate(data)
    with localcontext() as ctx:
        ctx.prec = 1
        for condition in ctx.traps:
            ctx.traps[condition] = True
        assert validate_refund_revision_amounts(parsed, catalog) is parsed
        assert parsed.model_dump(mode="json") == data
    for unavailable in [dict(ASSETS), catalog | {token.asset_id: ASSETS["USD"]}]:
        with pytest.raises(MoneyError, match="asset is inconsistent"):
            validate_refund_revision_amounts(parsed, unavailable)


@pytest.mark.parametrize("side", ["previous", "replacement"])
@pytest.mark.parametrize(
    "invalid",
    [
        {"amount": "11.001"},
        {"payment_amount": "12.001"},
        {"fees": [fee(amount="0.000000001")]},
    ],
)
def test_asset_scale_checked_on_both_sides_and_every_fee(side, invalid):
    parsed = RefundRevisionResponse.model_validate(response(**{side: fact(**invalid)}))
    with pytest.raises(MoneyError):
        validate_refund_revision_amounts(parsed, ASSETS)


@pytest.mark.parametrize("side", ["previous", "replacement"])
def test_same_asset_cash_and_principal_must_match_in_actual_units(side):
    data = response(**{side: fact(asset_id="USD", amount="12.01", payment_amount="12.00")})
    with pytest.raises(MoneyError, match="cash and principal differ"):
        validate_refund_revision_amounts(RefundRevisionResponse.model_validate(data), ASSETS)


def encoded(value):
    return json.dumps(value, ensure_ascii=False).encode()


def test_legacy_creation_contract_and_frozen_three_envelopes():
    original = replacement(id=uid(5), payment_id=uid(6), expected_version=1)
    old = BusinessRefundCreate.model_validate(original)
    assert _v2_hash("business_refund", uid(7), old, encoded(original)) == (
        "5c136fabc70d6fbe30d42ebbb8cd455f88ffd7cf9576083e56ac98f4852aae93"
    )
    old_json = {
        "kind": "business_refund",
        "id": uid(5),
        "ledger_id": uid(7),
        "payment_id": uid(6),
        "operation_id": uid(9),
        "journal_id": uid(21),
        "account_id": uid(1),
        "asset_id": "EUR",
        "amount": "11.00",
        "payment_asset_id": "USD",
        "payment_amount": "12.00",
        "transaction_date": "2031-02-03",
        "fees": [],
        "allocation_return_ids": [],
    }
    assert BusinessRefundResponse.model_validate(old_json).model_dump(mode="json") == old_json
    with pytest.raises(ValidationError):
        BusinessRefundResponse.model_validate(old_json | {"reason": "Fictional revision"})
    for action, model, fixed in [
        (
            "correct",
            RefundCorrectionCreate,
            "e3ad8b9e4acad28c0b51afdaa970010598754e79e2f5aa91898f9a367f0e07e5",
        ),
        (
            "cancel",
            RefundCancellationCreate,
            "e62a84eb6276cda8e1e3195add5279899e7610548b6c5726f6cdd1b4244c1866",
        ),
    ]:
        data = request(action)
        parsed = model.model_validate(data)
        assert refund_revision_hash_v2(action, uid(7), uid(5), parsed) == fixed
        assert (
            refund_revision_hash_v2(action, uid(7), uid(5), parsed, raw_body=encoded(data)) == fixed
        )


def test_raw_hash_distinguishes_scope_reason_spellings_and_complete_array_order():
    repl = replacement(
        fees=[fee(), fee(amount="0.00000002")], allocation_return_ids=[uid(11), uid(12)]
    )
    data = request("correct", replacement=repl)
    parsed = RefundCorrectionCreate.model_validate(data)

    def digest(body, action="correct", ledger=7, refund=5):
        return refund_revision_hash_v2(
            action, uid(ledger), uid(refund), parsed, raw_body=encoded(body)
        )

    expected = digest(data)
    formatted = json.dumps(data, sort_keys=True, indent=2).encode()
    assert (
        refund_revision_hash_v2("correct", uid(7), uid(5), parsed, raw_body=formatted) == expected
    )
    for delta in [
        {"amount": "11.0"},
        {"payment_amount": "12.0"},
        {"reason": "\x85" + repl["reason"]},
        {"fees": list(reversed(repl["fees"]))},
        {"allocation_return_ids": list(reversed(repl["allocation_return_ids"]))},
    ]:
        assert digest(data | {"replacement": repl | delta}) != expected
    assert digest(data, action="cancel") != expected
    assert digest(data, ledger=100) != expected
    assert digest(data, refund=100) != expected
    assert digest(data | {"reason": " " + data["reason"]}) != expected
    for field in ["fees", "allocation_return_ids"]:
        empty = request("correct", replacement=replacement(**{field: []}))
        omitted = request("correct")
        assert RefundCorrectionCreate.model_validate(
            omitted
        ) == RefundCorrectionCreate.model_validate(empty)
        assert digest(empty) != digest(omitted)


@pytest.mark.parametrize(
    "action,raw",
    [
        ("create", b"{}"),
        ("cancel", b"["),
        ("cancel", b"[]"),
        ("correct", b"null"),
        ("correct", b'{"expected_version":NaN}'),
        ("cancel", b"\xff"),
    ],
)
def test_invalid_raw_envelopes_fail_as_client_errors(action, raw):
    with pytest.raises(LedgerError) as caught:
        refund_revision_hash_v2(action, uid(7), uid(5), None, raw_body=raw)
    assert caught.value.code == "invalid_request"
    assert caught.value.status == 422
