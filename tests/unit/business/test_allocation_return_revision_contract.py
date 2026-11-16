"""Fictional full group transitions, exact two-asset facts and scoped permanent hashes."""

import json
from copy import deepcopy
from dataclasses import replace
from decimal import localcontext
from uuid import uuid4

import pytest
from coinpup_api.business.allocation_return_revision_hash import allocation_return_revision_hash_v2
from coinpup_api.business.allocation_return_revision_schemas import (
    AllocationReturnCancellationCreate,
    AllocationReturnCorrectionCreate,
    AllocationReturnRevisionResponse,
    validate_return_revision_amounts,
)
from coinpup_api.ledger.assets import ASSETS
from coinpup_api.ledger.errors import MoneyError
from coinpup_api.ledger.service import LedgerError
from pydantic import ValidationError


def command():
    return dict(
        expected_version=2,
        reason="  Fictional revision 中文  ",
        replacement=dict(
            return_date="2026-11-04",
            reason=" Fictional replacement ",
            lines=[
                dict(
                    source_allocation_line_id=str(uuid4()),
                    expected_version=1,
                    payment_amount="3.00",
                    document_amount="0.00000001",
                )
            ],
        ),
    )


def member(action, old=None, new=None):
    def fact(no, v):
        return dict(
            line_no=no,
            operation_version=v,
            journal_id=uuid4(),
            payment_amount="3.000000000000000000",
            document_amount="0.000000010000000000",
        )

    v = 1 if action == "create" else 2
    return dict(
        action=action,
        source_allocation_line_id=uuid4(),
        source_operation_id=uuid4(),
        source_version=1,
        source_return_no=7,
        document_id=uuid4(),
        document_line_id=uuid4(),
        document_asset_id="BTC",
        operation_id=uuid4(),
        operation_version=v,
        previous=fact(old, 1) if old else None,
        reversal_journal_id=uuid4() if old else None,
        replacement=fact(new, v) if new else None,
    )


def receipt():
    return dict(
        kind="business_allocation_return_revision",
        return_id=uuid4(),
        revision_id=uuid4(),
        ledger_id=uuid4(),
        payment_id=uuid4(),
        payment_asset_id="USD",
        previous_version=2,
        version=3,
        action="correct",
        reason="Fictional correction",
        previous=dict(return_date="2026-11-01", reason="Fictional original"),
        replacement=dict(return_date="2026-11-02", reason="Fictional replacement"),
        members=[member("create", new=1), member("correct", 2, 2), member("cancel", 1)],
    )


def test_original_raw_json_binds_spelling_order_group_ledger_and_action():
    body = command()
    body["replacement"]["lines"].append(
        body["replacement"]["lines"][0] | {"source_allocation_line_id": str(uuid4())}
    )
    payload = AllocationReturnCorrectionCreate.model_validate(body)
    assert (
        payload.reason == "Fictional revision 中文"
        and payload.replacement.reason == "Fictional replacement"
    )
    group, ledger = uuid4(), uuid4()

    def hashed(raw, action="correct", *, scope=ledger, target=group):
        return allocation_return_revision_hash_v2(
            action, scope, target, payload, raw_body=json.dumps(raw, ensure_ascii=False).encode()
        )

    key = hashed(body)
    assert key == hashed(dict(reversed(list(body.items()))))
    changed = deepcopy(body)
    changed["replacement"]["lines"][0]["payment_amount"] = "3"
    reordered = deepcopy(body)
    reordered["replacement"]["lines"].reverse()
    assert (
        len(
            {
                key,
                hashed(changed),
                hashed(reordered),
                hashed(body, scope=uuid4()),
                hashed(body, target=uuid4()),
                hashed(body, action="cancel"),
                hashed(body | {"reason": payload.reason}),
            }
        )
        == 7
    )
    for raw in (b"[]", b"NaN", b"NOT-JSON"):
        with pytest.raises(LedgerError):
            allocation_return_revision_hash_v2("correct", ledger, group, payload, raw_body=raw)


@pytest.mark.parametrize(
    "fault",
    [
        "duplicate",
        "absent",
        "201",
        "invalid-version",
        "overflow",
        "zero",
        "negative",
        "float",
        "precision",
        "path-identity",
        "blank",
        "null",
    ],
)
def test_reject_incomplete_or_inexact_replacement(fault):
    body = command()
    fact = body["replacement"]
    item = fact["lines"][0]
    if fault == "duplicate":
        fact["lines"] *= 2
    if fault == "absent":
        fact["lines"] = []
    if fault == "201":
        fact["lines"] = [item | {"source_allocation_line_id": str(uuid4())} for _ in range(201)]
    if fault == "invalid-version":
        item["expected_version"] = 2
    if fault == "overflow":
        body["expected_version"] = 2147483647
    if fault in ("zero", "negative", "float", "precision"):
        item["document_amount"] = {
            "zero": "0",
            "negative": "-1",
            "float": 1.1,
            "precision": "0." + "0" * 18 + "1",
        }[fault]
    if fault == "path-identity":
        body["payment_id"] = str(uuid4())
    if fault == "blank":
        fact["reason"] = "    "
    if fault == "null":
        body["reason"] = "bad" + chr(0) + "input"
    with pytest.raises(ValidationError):
        AllocationReturnCorrectionCreate.model_validate(body)


def test_complete_correction_and_terminal_cancellation_keep_original_facts_and_asset_scales():
    raw = receipt()
    value = AllocationReturnRevisionResponse.model_validate(raw)
    with localcontext() as c:
        c.prec = 1
        assert (
            validate_return_revision_amounts(
                value, ASSETS | {"BTC": replace(ASSETS["BTC"], enabled=False)}
            )
            is value
        )
    assert AllocationReturnRevisionResponse.model_validate_json(value.model_dump_json()) == value
    end = raw | dict(
        action="cancel", replacement=None, members=[member("cancel", 1), member("cancel", 2)]
    )
    assert AllocationReturnRevisionResponse.model_validate(end).replacement is None
    with pytest.raises(ValidationError):
        AllocationReturnCancellationCreate.model_validate(command())
    assert AllocationReturnCancellationCreate(expected_version=1, reason=" 虚构 ").reason == "虚构"


@pytest.mark.parametrize(
    "fault",
    [
        "reused-journal",
        "missing-reversal",
        "bad-position",
        "duplicate-source",
        "source-is-member",
        "trailing-create",
        "missing-half",
        "skipped-version",
        "wrong-fact-version",
        "member-runs-ahead",
        "revive",
        "no-prior",
        "same-asset",
        "wrong-scale",
        "wrong-identity",
    ],
)
def test_incomplete_or_corrupted_frozen_history_is_rejected(fault):
    raw = receipt()
    new, retained, removed = raw["members"]
    if fault == "reused-journal":
        new["replacement"]["journal_id"] = retained["reversal_journal_id"]
    if fault == "missing-reversal":
        removed["reversal_journal_id"] = None
    if fault == "bad-position":
        new["replacement"]["line_no"] = 2
    if fault == "duplicate-source":
        new["source_allocation_line_id"] = removed["source_allocation_line_id"]
    if fault == "source-is-member":
        retained["operation_id"] = new["source_operation_id"]
    if fault == "trailing-create":
        raw["members"] = [removed, new, retained]
    if fault == "missing-half":
        del retained["previous"]["document_amount"]
    if fault == "skipped-version":
        raw["version"] = 4
    if fault == "wrong-fact-version":
        new["replacement"]["operation_version"] = 2
    if fault == "member-runs-ahead":
        retained.update(operation_version=5)
        retained["previous"]["operation_version"] = 4
        retained["replacement"]["operation_version"] = 5
    if fault == "revive":
        raw["action"] = "cancel"
    if fault == "no-prior":
        raw["members"] = [new]
    if fault == "same-asset":
        new["document_asset_id"] = "USD"
        new["replacement"]["document_amount"] = "2.00"
    if fault == "wrong-scale":
        new["replacement"]["document_amount"] = "0.000000001"
    catalog = ASSETS if fault != "wrong-identity" else ASSETS | {"BTC": ASSETS["USD"]}
    with pytest.raises((ValidationError, MoneyError)):
        validate_return_revision_amounts(
            AllocationReturnRevisionResponse.model_validate(raw), catalog
        )


def test_exact_maximum_size_disjoint_groups_preserve_400_distinct_transitions():
    raw = receipt()
    raw["members"] = [member("create", new=i) for i in range(1, 201)] + [
        member("cancel", i) for i in range(1, 201)
    ]
    assert len(AllocationReturnRevisionResponse.model_validate(raw).members) == 400
    raw["members"].append(member("cancel", 1))
    with pytest.raises(ValidationError):
        AllocationReturnRevisionResponse.model_validate(raw)
