"""Fictional complete transitions and original JSON remain exact without a write endpoint."""

import copy
import json
from dataclasses import replace
from decimal import localcontext
from uuid import uuid4

import pytest
from coinpup_api.business.credit_hash import credit_hash_v2
from coinpup_api.business.credit_revision_hash import credit_revision_hash_v2
from coinpup_api.business.credit_revision_schemas import (
    CreditCancellationCreate,
    CreditCorrectionCreate,
    CreditRevisionResponse,
    validate_credit_revision_amounts,
)
from coinpup_api.business.credit_schemas import BusinessCreditCreate
from coinpup_api.ledger.assets import ASSETS, AssetDefinition
from coinpup_api.ledger.errors import MoneyError
from coinpup_api.ledger.idempotency import _canonical_hash, revision_hash_v2
from coinpup_api.ledger.service import LedgerError
from pydantic import ValidationError


def request():
    return dict(
        expected_version=5,
        reason="Fictional correction 虚构更正",
        replacement=dict(
            credit_date="2026-11-05",
            reason="Fictional replacement fact",
            lines=[
                dict(
                    source_line_id=str(uuid4()),
                    net_amount="17.23",
                    tax_amount="2.77",
                    recognition_date="2026-10-20",
                )
            ],
        ),
    )


def member(action="correct", previous=1, current=1):
    old, new = action != "create", action != "cancel"
    return dict(
        action=action,
        source_line_id=str(uuid4()),
        source_operation_id=str(uuid4()),
        operation_id=str(uuid4()),
        operation_version=3 if old else 1,
        previous_line_no=previous if old else None,
        previous_operation_version=2 if old else None,
        previous_journal_id=str(uuid4()) if old else None,
        reversal_journal_id=str(uuid4()) if old else None,
        previous_net_amount="9.000000000000000000" if old else None,
        previous_tax_amount="1.000000000000000000" if old else None,
        previous_total_amount="10.000000000000000000" if old else None,
        previous_recognition_date="2026-10-19" if old else None,
        line_no=current if new else None,
        journal_id=str(uuid4()) if new else None,
        net_amount="17.230000000000000000" if new else None,
        tax_amount="2.770000000000000000" if new else None,
        total_amount="20.000000000000000000" if new else None,
        recognition_date="2026-10-20" if new else None,
    )


def receipt(action="correct"):
    return dict(
        kind="business_credit_revision",
        credit_id=str(uuid4()),
        revision_id=str(uuid4()),
        ledger_id=str(uuid4()),
        document_id=str(uuid4()),
        asset_id="USD",
        previous_version=5,
        version=6,
        action=action,
        reason="Fictional group revision",
        previous_credit_date="2026-10-21",
        previous_credit_reason="Fictional previous fact",
        credit_date="2026-11-05" if action == "correct" else None,
        replacement_reason="Fictional replacement fact" if action == "correct" else None,
        members=[member("cancel" if action == "cancel" else "correct")],
    )


def test_request_keeps_manual_spelling_and_separates_revision_and_fact_reasons():
    body = request()
    body["reason"] = "  Fictional revision  "
    body["replacement"]["reason"] = "  Fictional fact  "
    body["replacement"]["lines"][0] |= dict(net_amount="0.00", tax_amount="2.770")
    parsed = CreditCorrectionCreate.model_validate(body)
    assert parsed.reason == "Fictional revision"
    assert parsed.replacement.reason == "Fictional fact"
    assert parsed.replacement.lines[0].tax_amount == "2.770"
    assert parsed.replacement.credit_date != parsed.replacement.lines[0].recognition_date
    # Actual asset precision, sources, dates and capacity still require locked service validation.
    assert CreditCancellationCreate(expected_version=1, reason="Fictional cancellation").reason


@pytest.mark.parametrize(
    "field,value",
    [
        ("expected_version", True),
        ("expected_version", 0),
        ("expected_version", 2147483647),
        ("reason", " "),
        ("reason", "Fictional\x00"),
        ("reason", "x" * 1001),
        ("id", str(uuid4())),
        ("document_id", str(uuid4())),
    ],
)
def test_requests_reject_unbounded_versions_reasons_and_moving_stable_identity(field, value):
    body = request()
    with pytest.raises(ValidationError):
        CreditCorrectionCreate.model_validate(body | {field: value})
    body.pop("replacement")
    with pytest.raises(ValidationError):
        CreditCancellationCreate.model_validate(body | {field: value})


@pytest.mark.parametrize(
    "field,value",
    [
        ("reason", " "),
        ("reason", "Fictional\x00"),
        ("reason", "x" * 2001),
        ("credit_date", "2026-02-30"),
        ("credit_date", "2026-1-1"),
        ("lines", []),
        ("asset_id", "EUR"),
        ("party_id", str(uuid4())),
    ],
)
def test_replacement_is_complete_nonempty_and_cannot_move_source_identity(field, value):
    body = request()
    body["replacement"][field] = value
    with pytest.raises(ValidationError):
        CreditCorrectionCreate.model_validate(body)


@pytest.mark.parametrize(
    "net,tax",
    [
        ("0", "0.00"),
        ("-0", "1"),
        ("-1", "2"),
        (1, "0"),
        ("1e2", "0"),
        ("100000000000000000000", "0"),
        ("0.0000000000000000001", "0"),
    ],
)
def test_replacement_amount_syntax_and_storage_components_are_strict(net, tax):
    body = request()
    body["replacement"]["lines"][0] |= dict(net_amount=net, tax_amount=tax)
    with pytest.raises(ValidationError):
        CreditCorrectionCreate.model_validate(body)


def test_replacement_sources_are_unique_and_at_most_200_and_cancel_has_no_replacement():
    body = request()
    line = body["replacement"]["lines"][0]
    body["replacement"]["lines"] = [dict(line, source_line_id=str(uuid4())) for _ in range(200)]
    assert len(CreditCorrectionCreate.model_validate(body).replacement.lines) == 200
    body["replacement"]["lines"].append(dict(line, source_line_id=str(uuid4())))
    with pytest.raises(ValidationError):
        CreditCorrectionCreate.model_validate(body)
    body["replacement"]["lines"] = [line, line]
    with pytest.raises(ValidationError):
        CreditCorrectionCreate.model_validate(body)
    with pytest.raises(ValidationError):
        CreditCancellationCreate.model_validate(request())


@pytest.mark.parametrize("action", ["correct", "cancel"])
def test_permanent_receipt_is_frozen_and_contains_complete_prior_facts(action):
    raw = receipt(action)
    with localcontext() as context:
        context.prec = 1
        parsed = CreditRevisionResponse.model_validate(raw)
        assert (
            validate_credit_revision_amounts(parsed, replace(ASSETS["USD"], enabled=False))
            is parsed
        )
    assert parsed.model_dump(mode="json") == raw
    assert parsed.members[0].previous_total_amount == "10.000000000000000000"
    assert parsed.version == 6 and parsed.members[0].operation_version == 3
    with pytest.raises(ValidationError):
        parsed.members[0].previous_total_amount = "20"
    with pytest.raises(ValidationError):
        parsed.version = 7


def test_full_200_to_200_exchange_has_400_transitions_without_equating_group_and_member_versions():
    raw = receipt()
    raw["members"] = [member("create", current=i) for i in range(1, 201)] + [
        member("cancel", previous=i) for i in range(1, 201)
    ]
    parsed = CreditRevisionResponse.model_validate(raw)
    assert len(parsed.members) == 400
    assert all(m.operation_version == 1 for m in parsed.members[:200])
    assert validate_credit_revision_amounts(parsed, ASSETS["USD"]) is parsed
    raw["members"].append(member("cancel", previous=201))
    with pytest.raises(ValidationError):
        CreditRevisionResponse.model_validate(raw)


@pytest.mark.parametrize(
    "action,field,value",
    [
        ("create", "previous_net_amount", "1"),
        ("create", "reversal_journal_id", str(uuid4())),
        ("create", "operation_version", 2),
        ("correct", "previous_journal_id", None),
        ("correct", "previous_total_amount", None),
        ("correct", "reversal_journal_id", None),
        ("correct", "previous_operation_version", 3),
        ("correct", "operation_version", 2147483648),
        ("correct", "operation_version", True),
        ("correct", "previous_operation_version", 0),
        ("correct", "line_no", 201),
        ("correct", "line_no", True),
        ("correct", "net_amount", "-0.00"),
        ("correct", "tax_amount", 2.77),
        ("correct", "total_amount", "0"),
        ("correct", "previous_total_amount", "0"),
        ("correct", "previous_recognition_date", "2026-02-30"),
        ("cancel", "journal_id", str(uuid4())),
        ("cancel", "net_amount", "0"),
    ],
)
def test_invalid_action_nullability_versions_positions_and_amounts_are_rejected(
    action, field, value
):
    raw = receipt("cancel" if action == "cancel" else "correct")
    if action == "create":
        raw["members"] = [member("create"), member("cancel")]
    raw["members"][0][field] = value
    with pytest.raises(ValidationError):
        CreditRevisionResponse.model_validate(raw)


@pytest.mark.parametrize(
    "fault",
    [
        "group_gap",
        "group_overflow",
        "member_ahead",
        "no_prior",
        "no_new",
        "cancel_new",
        "cancel_date",
        "missing_old",
        "duplicate_old",
        "new_gap",
        "order",
        "duplicate_source",
        "reuse_source_op",
        "reuse_journal",
        "reason",
        "omit_field",
    ],
)
def test_incomplete_or_inconsistent_group_receipts_cannot_pass(fault):
    raw = receipt()
    raw["members"] = [member("correct"), member("create", current=2), member("cancel", previous=2)]
    if fault == "group_gap":
        raw["version"] = 7
    elif fault == "group_overflow":
        raw.update(previous_version=2147483647, version=2147483648)
    elif fault == "member_ahead":
        raw["members"][0].update(previous_operation_version=6, operation_version=7)
    elif fault == "no_prior":
        raw["members"] = [member("create")]
    elif fault == "no_new":
        raw["members"] = [member("cancel")]
    elif fault == "cancel_new":
        raw.update(action="cancel", credit_date=None, replacement_reason=None)
    elif fault == "cancel_date":
        raw = receipt("cancel") | {"credit_date": "2026-11-05"}
    elif fault in {"missing_old", "duplicate_old"}:
        raw["members"][-1]["previous_line_no"] = 3 if fault == "missing_old" else 1
    elif fault == "new_gap":
        raw["members"][1]["line_no"] = 3
    elif fault == "order":
        raw["members"].reverse()
    elif fault == "duplicate_source":
        raw["members"][1]["source_line_id"] = raw["members"][0]["source_line_id"]
    elif fault == "reuse_source_op":
        raw["members"][1]["operation_id"] = raw["members"][0]["source_operation_id"]
    elif fault == "reuse_journal":
        raw["members"][1]["journal_id"] = raw["members"][0]["previous_journal_id"]
    elif fault == "reason":
        raw["previous_credit_reason"] = " "
    else:
        del raw["members"][0]["previous_tax_amount"]
    with pytest.raises(ValidationError):
        CreditRevisionResponse.model_validate(raw)


@pytest.mark.parametrize("prefix", ["previous_", ""])
def test_every_side_requires_actual_asset_precision_and_exact_components(prefix):
    raw = receipt()
    raw["members"][0].update(
        {
            prefix + "net_amount": "19.999",
            prefix + "tax_amount": "0.001",
            prefix + "total_amount": "20.000000000000000000",
        }
    )
    with pytest.raises(MoneyError, match="precision"):
        validate_credit_revision_amounts(CreditRevisionResponse.model_validate(raw), ASSETS["USD"])
    raw["members"][0][prefix + "net_amount"] = "19"
    raw["members"][0][prefix + "tax_amount"] = "0"
    with pytest.raises(MoneyError) as failure:
        validate_credit_revision_amounts(CreditRevisionResponse.model_validate(raw), ASSETS["USD"])
    assert failure.value.code == "credit_revision_integrity"


def test_full_historical_custom_asset_and_storage_extreme_remain_exact():
    raw = receipt()
    token = AssetDefinition("USDC", "token", 18, "fictionalnet", "fictional-token", False)
    raw["asset_id"] = token.asset_id
    raw["members"][0].update(
        net_amount="99999999999999999999.999999999999999998",
        tax_amount="0.000000000000000001",
        total_amount="99999999999999999999.999999999999999999",
    )
    parsed = CreditRevisionResponse.model_validate(raw)
    with localcontext() as context:
        context.prec = 1
        assert validate_credit_revision_amounts(parsed, token) is parsed
    for invalid in (ASSETS["USD"], None):
        with pytest.raises(MoneyError):
            validate_credit_revision_amounts(parsed, invalid)


def test_hash_binds_independent_route_group_ledger_and_untouched_original_json():
    body, ledger, group = request(), uuid4(), uuid4()
    payload = CreditCorrectionCreate.model_validate(body)
    raw = json.dumps(body, ensure_ascii=False, indent=2).encode()
    digest = credit_revision_hash_v2("correct", ledger, group, payload, raw_body=raw)
    assert digest == credit_revision_hash_v2("correct", ledger, group, payload)
    assert digest == credit_revision_hash_v2(
        "correct", ledger, group, payload, raw_body=json.dumps(body, sort_keys=True)
    )
    assert digest == _canonical_hash(
        dict(
            action="business_credit_correct",
            route="/api/v1/ledgers/{ledger_id}/business-credits/{credit_id}/corrections",
            ledger_id=str(ledger),
            credit_id=str(group),
            payload=body,
        )
    )
    for action, owner, identifier in (
        ("cancel", ledger, group),
        ("correct", uuid4(), group),
        ("correct", ledger, uuid4()),
    ):
        assert digest != credit_revision_hash_v2(action, owner, identifier, payload, raw_body=raw)
    assert digest != revision_hash_v2("correct", ledger, group, payload, raw_body=raw)
    original = BusinessCreditCreate(
        id=group, document_id=uuid4(), expected_version=2, **body["replacement"]
    )
    assert digest != credit_hash_v2(ledger, original, raw_body=raw)
    for field, value in (
        ("reason", body["reason"] + " "),
        ("expected_version", 6),
        ("replacement", None),
        ("extra", None),
        ("extra", []),
    ):
        assert digest != credit_revision_hash_v2(
            "correct", ledger, group, payload, raw_body=json.dumps(body | {field: value})
        )
    changed = copy.deepcopy(body)
    changed["replacement"]["lines"][0]["net_amount"] = "17.230"
    assert digest != credit_revision_hash_v2(
        "correct", ledger, group, payload, raw_body=json.dumps(changed)
    )
    changed["replacement"]["lines"].append(
        dict(changed["replacement"]["lines"][0], source_line_id=str(uuid4()))
    )
    one = credit_revision_hash_v2("correct", ledger, group, payload, raw_body=json.dumps(changed))
    changed["replacement"]["lines"].reverse()
    assert one != credit_revision_hash_v2(
        "correct", ledger, group, payload, raw_body=json.dumps(changed)
    )


@pytest.mark.parametrize("raw", [b"[]", b"null", b"{", b'{"value":NaN}', b'{"value":Infinity}'])
def test_bad_raw_json_never_gets_a_revision_digest(raw):
    with pytest.raises(LedgerError) as failure:
        credit_revision_hash_v2(
            "cancel",
            uuid4(),
            uuid4(),
            CreditCancellationCreate(expected_version=1, reason="Fictional"),
            raw_body=raw,
        )
    assert failure.value.code == "invalid_request"


def test_cancellation_digest_does_not_invent_defaults_and_rejects_unknown_action():
    body = dict(expected_version=1, reason="Fictional cancellation")
    payload, ledger, group = CreditCancellationCreate(**body), uuid4(), uuid4()
    assert credit_revision_hash_v2("cancel", ledger, group, payload) == _canonical_hash(
        dict(
            action="business_credit_cancel",
            route="/api/v1/ledgers/{ledger_id}/business-credits/{credit_id}/cancellations",
            ledger_id=str(ledger),
            credit_id=str(group),
            payload=body,
        )
    )
    with pytest.raises(LedgerError):
        credit_revision_hash_v2("replace", ledger, group, payload)
