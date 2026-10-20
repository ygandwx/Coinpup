"""Fictional credits retain manual components, separate dates and exact original intent."""

import copy
import json
from decimal import localcontext
from uuid import uuid4

import pytest
from coinpup_api.business.credit_hash import credit_hash_v2
from coinpup_api.business.credit_schemas import BusinessCreditCreate, BusinessCreditResponse
from coinpup_api.ledger.idempotency import _v2_hash
from coinpup_api.ledger.service import LedgerError
from pydantic import ValidationError


def request():
    return dict(
        id=str(uuid4()),
        document_id=str(uuid4()),
        expected_version=2,
        credit_date="2026-10-21",
        reason="Fictional credit 虚构贷项",
        lines=[
            dict(
                source_line_id=str(uuid4()),
                net_amount="17.23",
                tax_amount="2.77",
                recognition_date="2026-10-20",
            )
            for _ in range(2)
        ],
    )


def response():
    body = request()
    return dict(
        kind="business_credit",
        id=body["id"],
        ledger_id=str(uuid4()),
        document_id=body["document_id"],
        source_version=body["expected_version"],
        asset_id="USD",
        credit_date=body["credit_date"],
        postings=[
            dict(
                **line,
                line_no=index,
                source_operation_id=str(uuid4()),
                total_amount="20.000000000000000000",
                operation_id=str(uuid4()),
                journal_id=str(uuid4()),
            )
            for index, line in enumerate(body["lines"], 1)
        ],
    )


def test_command_preserves_manual_spelling_and_explicit_separate_dates():
    body = request()
    body["lines"][0] |= {"net_amount": "0.00", "tax_amount": "2.770"}
    saved = BusinessCreditCreate.model_validate(body).model_dump(mode="json")
    assert saved == body
    assert saved["credit_date"] != saved["lines"][0]["recognition_date"]


@pytest.mark.parametrize(
    "field,value",
    [
        ("id", "fictional"),
        ("expected_version", True),
        ("expected_version", 0),
        ("expected_version", 2147483648),
        ("credit_date", "2026-1-1"),
        ("credit_date", "2026-02-30"),
        ("credit_date", "2026-10-21T00:00:00"),
        ("reason", " "),
        ("reason", "fictional\x00"),
        ("reason", "a" * 2001),
        ("lines", []),
        ("account_id", str(uuid4())),
        ("cash_amount", "20"),
    ],
)
def test_command_rejects_malformed_header_and_cash_fields(field, value):
    with pytest.raises(ValidationError):
        BusinessCreditCreate.model_validate(request() | {field: value})


@pytest.mark.parametrize(
    "field,value",
    [
        ("net_amount", 1),
        ("tax_amount", True),
        ("net_amount", "1e2"),
        ("net_amount", "01"),
        ("tax_amount", "+1"),
        ("recognition_date", "2026-02-30"),
        ("recognition_date", None),
        ("category_id", str(uuid4())),
        ("project_id", str(uuid4())),
        ("asset_id", "EUR"),
    ],
)
def test_command_rejects_malformed_manual_line_and_reclassification(field, value):
    body = request()
    body["lines"][0][field] = value
    with pytest.raises(ValidationError):
        BusinessCreditCreate.model_validate(body)


def test_source_membership_is_unique_and_bounded_at_200():
    body = request()
    line = body["lines"][0]
    body["lines"] = [dict(line, source_line_id=str(uuid4())) for _ in range(200)]
    assert len(BusinessCreditCreate.model_validate(body).lines) == 200
    body["lines"].append(dict(line, source_line_id=str(uuid4())))
    with pytest.raises(ValidationError):
        BusinessCreditCreate.model_validate(body)
    body["lines"] = [line, line]
    with pytest.raises(ValidationError):
        BusinessCreditCreate.model_validate(body)


def test_complete_receipt_preserves_18_place_strings_and_is_immutable():
    saved = response()
    with localcontext() as context:
        context.prec = 1
        parsed = BusinessCreditResponse.model_validate(saved)
    assert parsed.model_dump(mode="json") == saved
    with pytest.raises(ValidationError):
        parsed.source_version = 3
    with pytest.raises(ValidationError):
        parsed.postings[0].net_amount = "1"


@pytest.mark.parametrize(
    "net,tax,total",
    [
        ("0", "0.01", "0.010000000000000000"),
        ("0.01", "0", "0.01"),
        ("0", "0.000000000000000001", "0.000000000000000001"),
        (
            "99999999999999999999.999999999999999998",
            "0.000000000000000001",
            "99999999999999999999.999999999999999999",
        ),
    ],
)
def test_receipt_zero_side_and_extremes_are_exact(net, tax, total):
    saved = response()
    saved["postings"][0] |= dict(net_amount=net, tax_amount=tax, total_amount=total)
    with localcontext() as context:
        context.prec = 1
        parsed = BusinessCreditResponse.model_validate(saved)
    assert parsed.postings[0].total_amount == total


@pytest.mark.parametrize(
    "field,value",
    [
        ("net_amount", "-1"),
        ("tax_amount", "-0.00"),
        ("total_amount", "0"),
        ("total_amount", "19.99"),
        ("net_amount", "100000000000000000000"),
        ("tax_amount", "0.0000000000000000001"),
        ("net_amount", 17.23),
        ("line_no", 0),
        ("line_no", 3),
        ("recognition_date", "2026-10-21T00:00:00"),
    ],
)
def test_receipt_rejects_bad_components_dates_and_missing_positions(field, value):
    saved = response()
    saved["postings"][0][field] = value
    with pytest.raises(ValidationError):
        BusinessCreditResponse.model_validate(saved)


@pytest.mark.parametrize(
    "field", ["source_line_id", "source_operation_id", "operation_id", "journal_id"]
)
def test_receipt_rejects_duplicate_original_or_new_identities(field):
    saved = response()
    saved["postings"][1][field] = saved["postings"][0][field]
    with pytest.raises(ValidationError):
        BusinessCreditResponse.model_validate(saved)


def test_receipt_cannot_reuse_a_different_original_operation_or_reorder_members():
    saved = response()
    saved["postings"][0]["operation_id"] = saved["postings"][1]["source_operation_id"]
    with pytest.raises(ValidationError):
        BusinessCreditResponse.model_validate(saved)
    saved = response()
    saved["postings"].reverse()
    with pytest.raises(ValidationError):
        BusinessCreditResponse.model_validate(saved)


def test_v2_binds_route_ledger_and_raw_manual_source_intent():
    body, ledger = request(), uuid4()
    payload = BusinessCreditCreate.model_validate(body)
    raw = json.dumps(body, ensure_ascii=False, indent=2).encode()
    digest = credit_hash_v2(ledger, payload, raw_body=raw)
    assert digest == credit_hash_v2(ledger, payload)
    assert digest == credit_hash_v2(ledger, payload, raw_body=json.dumps(body, sort_keys=True))
    assert digest != credit_hash_v2(uuid4(), payload, raw_body=raw)
    for action in ("business_refund", "business_payment", "business_allocation_return"):
        assert digest != _v2_hash(action, ledger, payload, raw)
    changes = []
    for field, value in (
        ("document_id", str(uuid4())),
        ("expected_version", 3),
        ("credit_date", "2026-10-22"),
        ("reason", body["reason"] + " "),
    ):
        changes.append(body | {field: value})
    for field, value in (
        ("source_line_id", str(uuid4())),
        ("net_amount", "17.230"),
        ("tax_amount", "2.770"),
        ("recognition_date", "2026-10-21"),
    ):
        changed = copy.deepcopy(body)
        changed["lines"][0][field] = value
        changes.append(changed)
    changes.append(body | {"lines": list(reversed(body["lines"]))})
    for changed in changes:
        assert digest != credit_hash_v2(ledger, payload, raw_body=json.dumps(changed))


@pytest.mark.parametrize("raw", [b"[]", b"null", b"{", b'{"amount":NaN}'])
def test_v2_rejects_invalid_nonobject_and_nonfinite_raw_json(raw):
    with pytest.raises(LedgerError) as failure:
        credit_hash_v2(uuid4(), BusinessCreditCreate.model_validate(request()), raw_body=raw)
    assert failure.value.code == "invalid_request"
