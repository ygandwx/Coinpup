"""Closed periods reject new facts, never successful original intents."""

import json
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
from coinpup_api.ledger.models import Entity
from coinpup_api.ledger.period_models import PeriodAudit, PeriodReceipt
from coinpup_api.ledger.period_schemas import PeriodChange
from coinpup_api.ledger.period_service import PeriodService
from coinpup_api.ledger.posting_schemas import (
    CancellationCreate,
    CorrectionCreate,
    ExchangeCreate,
    TransferCreate,
)
from coinpup_api.ledger.schemas import AccountCreate, EntityCreate
from coinpup_api.ledger.service import LedgerError
from sqlalchemy import event, select, update
from sqlalchemy.orm import Session

from tests.integration.ledger.test_account_classes import snapshot
from tests.integration.ledger.test_posting_service_database import classified, opening
from tests.integration.ledger.test_posting_service_database import ledger_setup as ledger_setup

pytestmark = pytest.mark.integration
CUTOFF = "2026-01-31"


def change(s, version=1, cutoff=CUTOFF, action="close", key=None, **kwargs):
    return PeriodService(s["engine"]).change(
        s["owner"],
        s["ledger"],
        PeriodChange(
            action=action,
            closed_through=cutoff,
            expected_version=version,
            reason="Fictional period verification",
        ),
        key or str(uuid4()),
        **kwargs,
    )


def command(s, kind, day):
    if kind == "opening":
        return opening(s, transaction_date=day)
    fee = {
        "account_id": s["account"].id,
        "asset_id": "USD",
        "amount": "1.00",
        "category_id": s["expenses"][0].id,
    }
    if kind in {"income", "expense"}:
        return classified(s, kind=kind, transaction_date=day, recognition_date=day, fees=[fee])
    other = s["structure"].create_account(
        s["owner"],
        s["ledger"],
        AccountCreate(name="Fictional period destination", kind="bank", asset_ids=["USD", "EUR"]),
    )
    common = dict(
        source_account_id=s["account"].id,
        destination_account_id=other.id,
        transaction_date=day,
        fees=[fee],
    )
    return (
        TransferCreate(**common, asset_id="USD", amount="2.00")
        if kind == "transfer"
        else ExchangeCreate(
            **common,
            source_asset_id="USD",
            source_amount="2.00",
            destination_asset_id="EUR",
            destination_amount="1.80",
        )
    )


@pytest.mark.parametrize("kind", ["opening", "income", "expense", "transfer", "exchange"])
@pytest.mark.parametrize("day", ["2026-01-30", CUTOFF, "2026-02-01"])
def test_inclusive_cutoff_covers_all_commands_and_fees(ledger_setup, kind, day):
    s = ledger_setup
    payload = command(s, kind, day)
    change(s)
    before = snapshot(s)
    call = getattr(s["posting"], f"post_{kind}")
    if day <= CUTOFF:
        with pytest.raises(LedgerError) as failure:
            call(s["owner"], s["ledger"], payload, "fictional-period-post")
        assert failure.value.code == "period_closed" and failure.value.status == 409
        assert snapshot(s) == before
    else:
        result = call(s["owner"], s["ledger"], payload, "fictional-period-post")
        change(s, 2, "2026-02-28")
        after = snapshot(s)
        assert call(s["owner"], s["ledger"], payload, "fictional-period-post") == result
        assert snapshot(s) == after


@pytest.mark.parametrize("which", ["transaction_date", "recognition_date"])
def test_either_date_blocks_even_when_other_date_is_open(ledger_setup, which):
    s = ledger_setup
    payload = classified(
        s,
        **({"transaction_date": "2026-02-02", "recognition_date": "2026-02-02"} | {which: CUTOFF}),
    )
    change(s)
    before = snapshot(s)
    with pytest.raises(LedgerError, match="Reopen"):
        s["posting"].post_expense(s["owner"], s["ledger"], payload, "fictional-dual-date")
    assert snapshot(s) == before


@pytest.mark.parametrize("original_closed", [True, False])
def test_correction_checks_original_and_replacement_before_writes(ledger_setup, original_closed):
    s = ledger_setup
    original = "2026-01-30" if original_closed else "2026-02-02"
    replacement = "2026-02-02" if original_closed else "2026-01-30"
    receipt = s["posting"].post_expense(
        s["owner"],
        s["ledger"],
        classified(s, transaction_date=original, recognition_date=original),
        "fictional-original",
    )
    payload = CorrectionCreate(
        expected_version=1,
        reason="Fictional correction",
        replacement={
            "kind": "expense",
            **classified(s, transaction_date=replacement, recognition_date=replacement).model_dump(
                mode="json", exclude_unset=True
            ),
        },
    )
    change(s)
    before = snapshot(s)
    with pytest.raises(LedgerError) as failure:
        s["posting"].correct_operation(
            s["owner"], s["ledger"], receipt.id, payload, "fictional-correct"
        )
    assert failure.value.code == "period_closed" and snapshot(s) == before
    change(s, 2, None, "reopen")
    corrected = s["posting"].correct_operation(
        s["owner"], s["ledger"], receipt.id, payload, "fictional-correct"
    )
    change(s, 3, "2026-02-28")
    before = snapshot(s)
    assert (
        s["posting"].correct_operation(
            s["owner"], s["ledger"], receipt.id, payload, "fictional-correct"
        )
        == corrected
    )
    assert snapshot(s) == before


def test_cancel_requires_reasoned_reopening_and_receipts_remain_permanent(ledger_setup):
    s = ledger_setup
    result = s["posting"].post_opening(s["owner"], s["ledger"], opening(s), "fictional-original")
    first = change(s, key="fictional-close")
    payload = CancellationCreate(expected_version=1, reason="Fictional cancellation")
    before = snapshot(s)
    with pytest.raises(LedgerError) as failure:
        s["posting"].cancel_operation(
            s["owner"], s["ledger"], result.id, payload, "fictional-cancel"
        )
    assert failure.value.code == "period_closed" and snapshot(s) == before
    change(s, 2, None, "reopen")
    cancelled = s["posting"].cancel_operation(
        s["owner"], s["ledger"], result.id, payload, "fictional-cancel"
    )
    change(s, 3)
    with s["engine"].begin() as c:
        c.execute(
            update(Entity).where(Entity.id == s["entity"].id).values(archived=True, version=2)
        )
    before = snapshot(s)
    assert change(s, key="fictional-close") == first
    assert (
        s["posting"].cancel_operation(
            s["owner"], s["ledger"], result.id, payload, "fictional-cancel"
        )
        == cancelled
    )
    assert snapshot(s) == before


@pytest.mark.parametrize(
    "action,cutoff,version,code",
    [
        ("close", None, 1, "invalid_period_transition"),
        ("reopen", None, 1, "invalid_period_transition"),
        ("close", CUTOFF, 2, "version_conflict"),
    ],
)
def test_invalid_initial_change_does_not_materialize_state(
    ledger_setup, action, cutoff, version, code
):
    s = ledger_setup
    before = snapshot(s)
    with pytest.raises(LedgerError) as failure:
        change(s, version, cutoff, action)
    assert failure.value.code == code and snapshot(s) == before


def test_stale_change_wrong_key_and_ownership_leave_no_writes(ledger_setup):
    s = ledger_setup
    change(s, key="fictional-close")
    before = snapshot(s)
    for action, cutoff, version, key, code in [
        ("reopen", None, 1, "new", "version_conflict"),
        ("close", "2026-01-01", 2, "new", "invalid_period_transition"),
        ("reopen", CUTOFF, 2, "new", "invalid_period_transition"),
        ("close", "2026-02-01", 1, "fictional-close", "idempotency_conflict"),
    ]:
        with pytest.raises(LedgerError) as failure:
            change(s, version, cutoff, action, key)
        assert failure.value.code == code
    with pytest.raises(LedgerError) as failure:
        change(s | {"ledger": uuid4()})
    assert failure.value.code == "not_found" and snapshot(s) == before


def test_concurrent_identical_close_and_reopen_are_once_only(ledger_setup):
    s = ledger_setup
    for version, cutoff, action in [(1, CUTOFF, "close"), (2, None, "reopen")]:
        with ThreadPoolExecutor(max_workers=2) as pool:
            replies = list(
                pool.map(lambda _, v=version, c=cutoff, a=action: change(s, v, c, a, a), range(2))
            )
        assert replies[0] == replies[1] and replies[0].version == version + 1
    with s["engine"].connect() as c:
        assert len(c.execute(select(PeriodAudit)).all()) == 2
        assert len(c.execute(select(PeriodReceipt)).all()) == 2


def test_failure_after_state_and_audit_rolls_back_every_table(ledger_setup):
    s = ledger_setup
    before = snapshot(s)

    def fail_receipt(session, *_):
        if any(isinstance(row, PeriodReceipt) for row in session.new):
            raise RuntimeError("Fictional receipt fault")

    event.listen(Session, "before_flush", fail_receipt)
    try:
        with pytest.raises(RuntimeError, match="Fictional receipt fault"):
            change(s)
    finally:
        event.remove(Session, "before_flush", fail_receipt)
    assert snapshot(s) == before


def test_real_pre_upgrade_v1_receipts_replay_across_close_reopen_and_reclose(
    legacy_v1_receipts, authenticated_client
):
    s = legacy_v1_receipts
    client, _, _ = authenticated_client
    for version, cutoff, action in [
        (1, "2099-12-31", "close"),
        (2, None, "reopen"),
        (3, "2099-12-31", "close"),
    ]:
        change(s, version, cutoff, action)
        before = snapshot(s)
        for case in s["cases"].values():
            response = client.post(
                f"/api/v1/ledgers/{s['ledger']}" + case["suffix"],
                content=json.dumps(case["body"]).encode(),
                headers={"Content-Type": "application/json", "Idempotency-Key": case["key"]},
            )
            assert response.status_code == case["status"] and response.json() == case["response"]
        assert snapshot(s) == before


def test_other_ledger_remains_open_and_reasoned_partial_reopen_is_inclusive(ledger_setup):
    s = ledger_setup
    change(s)
    other = s["structure"].create_entity(
        s["owner"],
        EntityCreate(kind="personal", name="Fictional independent period", base_asset_id="USD"),
    )
    account = s["structure"].create_account(
        s["owner"],
        other.ledger.id,
        AccountCreate(name="Fictional independent cash", kind="cash", asset_ids=["USD"]),
    )
    s["posting"].post_opening(
        s["owner"], other.ledger.id, opening(s, account_id=account.id), "fictional-independent"
    )
    change(s, 2, "2026-01-15", "reopen")
    before = snapshot(s)
    with pytest.raises(LedgerError) as failure:
        s["posting"].post_opening(
            s["owner"],
            s["ledger"],
            opening(s, transaction_date="2026-01-15"),
            "fictional-still-closed",
        )
    assert failure.value.code == "period_closed" and snapshot(s) == before
    s["posting"].post_opening(
        s["owner"], s["ledger"], opening(s, transaction_date="2026-01-16"), "fictional-reopened"
    )
