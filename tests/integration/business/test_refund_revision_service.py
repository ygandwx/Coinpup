"""Commit complete changes to a real refund, then replay against immutable stored 200s."""

import json
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from decimal import Decimal
from threading import Barrier
from uuid import uuid4

import pytest
from coinpup_api.business import refund_revisions as revisions
from coinpup_api.business.refund_revision_receipts import response_from_rows
from coinpup_api.business.refund_revision_schemas import (
    RefundCancellationCreate,
    RefundCorrectionCreate,
)
from coinpup_api.business.refund_revisions import BusinessRefundRevisionService
from coinpup_api.business.settlement_totals import payment_totals
from coinpup_api.ledger.models import AssetRecord, CommandReceipt, FinancialOperation, Journal
from coinpup_api.ledger.service import LedgerError
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from tests.integration.business.test_draft_service import drafts as drafts
from tests.integration.business.test_refund_service import refund, setup_refund
from tests.integration.ledger.test_account_classes import snapshot
from tests.integration.ledger.test_period_transactions import change as change_period
from tests.integration.ledger.test_posting_service_database import ledger_setup as ledger_setup
from tests.integration.ledger.test_posting_service_database import opening

pytestmark = pytest.mark.integration


def correction(s, original, version=1, **changes):
    replacement = dict(
        account_id=str(original.account_id),
        asset_id="USD",
        amount="15",
        payment_amount="15",
        transaction_date="2026-10-04",
        reason="Fictional amended cash facts",
        fees=[],
        allocation_return_ids=[],
    )
    return {
        "expected_version": version,
        "reason": "Fictional explicit correction",
        "replacement": replacement | changes,
    }


def submit(s, original, action, data, key, *, ledger=None, refund_id=None):
    cls = RefundCorrectionCreate if action == "correct" else RefundCancellationCreate
    payload = cls.model_validate(data)
    return getattr(BusinessRefundRevisionService(s["engine"]), action)(
        s["owner"],
        ledger or s["ledger"],
        refund_id or original.id,
        payload,
        key,
        raw_body=json.dumps(data, ensure_ascii=False).encode(),
    )


@pytest.mark.parametrize("direction", ["receipt", "payout"])
def test_multi_asset_corrections_own_one_operation_and_replay_each_permanent_version(
    drafts, direction
):
    s = drafts
    _, _, body = setup_refund(s, direction, asset="EUR", amount="18.37")
    original = refund(s, body)
    s["posting"].post_opening(
        s["owner"], s["ledger"], opening(s, "1", "BTC"), "fictional-third-asset"
    )
    fees = [
        dict(
            account_id=str(body.account_id),
            category_id=str(s["expenses"][0].id),
            asset_id="BTC",
            amount=q,
        )
        for q in ("0.00000001", "0.00000002")
    ]
    first = correction(s, original, asset_id="EUR", amount="14.77", fees=fees)
    one = submit(s, original, "correct", first, "fictional-correct-1")
    assert one.version == 2 and one.previous_journal_id == original.journal_id
    assert one.previous.allocation_return_ids == original.allocation_return_ids
    assert one.replacement.allocation_return_ids == () and [
        fee.amount for fee in one.replacement.fees
    ] == [fee["amount"] for fee in fees]
    second = correction(
        s,
        original,
        2,
        amount="0.01",
        payment_amount="0.01",
        allocation_return_ids=[str(x) for x in original.allocation_return_ids],
    )
    two = submit(s, original, "correct", second, "fictional-correct-2")
    assert (
        two.previous == one.replacement
        and two.replacement.allocation_return_ids == original.allocation_return_ids
    )
    with s["engine"].connect() as c:
        assert payment_totals(c, s["ledger"], [original.payment_id])[original.payment_id] == (
            Decimal("80"),
            Decimal("0.01"),
        )
    end = dict(expected_version=3, reason="Fictional terminal cancellation")
    three = submit(s, original, "cancel", end, "fictional-cancel")
    assert (
        three.previous == two.replacement
        and three.replacement is None
        and three.replacement_journal_id is None
    )
    before = snapshot(s)
    for act, cmd, key, saved in (
        ("correct", first, "fictional-correct-1", one),
        ("correct", second, "fictional-correct-2", two),
        ("cancel", end, "fictional-cancel", three),
    ):
        assert submit(s, original, act, cmd, key).model_dump(mode="json") == saved.model_dump(
            mode="json"
        )
    assert refund(s, body) == original
    with Session(s["engine"]) as session:
        assert response_from_rows(session, one.revision_id) == one
        op = session.get(FinancialOperation, original.operation_id)
        assert (op.version, op.status, op.current_journal_id) == (
            4,
            "cancelled",
            two.replacement_journal_id,
        )
        assert (
            session.scalar(
                select(func.count(Journal.id)).where(
                    Journal.operation_id == op.id, Journal.sealed.is_(True)
                )
            )
            == 6
        )
        assert payment_totals(session, s["ledger"], [original.payment_id])[original.payment_id] == (
            Decimal("80"),
            Decimal("0"),
        )
    with pytest.raises(LedgerError) as terminal:
        submit(s, original, "cancel", end | {"expected_version": 4}, "fictional-after-terminal")
    assert terminal.value.code == "refund_cancelled" and snapshot(s) == before


@pytest.mark.parametrize(
    "fault",
    [
        "foreign_ledger",
        "unknown_refund",
        "foreign_evidence",
        "capacity",
        "precision",
        "version",
        "period",
        "cash_date",
        "raw_spelling",
    ],
)
def test_scope_capacity_precision_dates_and_exact_original_json_have_zero_failure_writes(
    drafts, fault
):
    s = drafts
    _, _, body = setup_refund(s)
    original = refund(s, body)
    key = "fictional-checked-command"
    extra = {}
    # On this payment, 80 remains allocated and this refund occupies the remaining 20.
    changes = {
        "capacity": dict(amount="20.01", payment_amount="20.01"),
        "precision": dict(amount="0.001", payment_amount="0.001"),
        "cash_date": dict(transaction_date="2026-01-01"),
        "foreign_evidence": dict(allocation_return_ids=[str(uuid4())]),
    }
    data = correction(s, original, 3 if fault == "version" else 1, **changes.get(fault, {}))
    if fault == "period":
        change_period(s, cutoff="2026-10-02")
    if fault == "foreign_ledger":
        extra["ledger"] = uuid4()
    if fault == "unknown_refund":
        extra["refund_id"] = uuid4()
    if fault == "raw_spelling":
        first = submit(s, original, "correct", data, key)
        data = deepcopy(data)
        data["replacement"]["amount"] = "15.0"
        assert first.version == 2
    before = snapshot(s)
    with pytest.raises(LedgerError) as rejected:
        submit(s, original, "correct", data, key, **extra)
    assert (
        rejected.value.code
        == {
            "foreign_ledger": "not_found",
            "unknown_refund": "not_found",
            "foreign_evidence": "not_found",
            "capacity": "refund_exceeds_payment",
            "precision": "amount_precision",
            "version": "version_conflict",
            "period": "period_closed",
            "cash_date": "refund_date",
            "raw_spelling": "idempotency_conflict",
        }[fault]
    )
    assert snapshot(s) == before


def test_disabled_historical_cash_and_fees_can_be_reversed_and_replayed(drafts):
    s = drafts
    _, _, body = setup_refund(s, asset="EUR", amount="19.99")
    original = refund(s, body)
    with s["engine"].begin() as c:
        c.execute(update(AssetRecord).where(AssetRecord.asset_id == "EUR").values(enabled=False))
    try:
        same = correction(s, original, asset_id="EUR", amount="14.77")
        before = snapshot(s)
        with pytest.raises(LedgerError) as rejected:
            submit(s, original, "correct", same, "fictional-disabled")
        assert rejected.value.code == "asset_disabled" and snapshot(s) == before
        cash = correction(s, original)
        saved = submit(s, original, "correct", cash, "fictional-use-usd")
        assert saved.previous.asset_id == "EUR" and saved.replacement.asset_id == "USD"
        before = snapshot(s)
        assert (
            submit(s, original, "correct", cash, "fictional-use-usd") == saved
            and snapshot(s) == before
        )
    finally:
        with s["engine"].begin() as c:
            c.execute(update(AssetRecord).where(AssetRecord.asset_id == "EUR").values(enabled=True))


def test_injected_interruption_after_rows_rolls_back_revision_operation_cash_and_notifications(
    drafts, monkeypatch
):
    s = drafts
    _, _, body = setup_refund(s)
    original = refund(s, body)
    data = correction(
        s, original, allocation_return_ids=[str(x) for x in original.allocation_return_ids]
    )
    before = snapshot(s)

    def interrupt(session, identifier):
        assert (
            session.scalar(
                select(func.count(Journal.id)).where(Journal.operation_id == original.operation_id)
            )
            == 3
        )
        raise RuntimeError("Fictional interruption after financial rows")

    with monkeypatch.context() as patch:
        patch.setattr(revisions, "response_from_rows", interrupt)
        with pytest.raises(RuntimeError, match="Fictional interruption"):
            submit(s, original, "correct", data, "fictional-interrupted")
    assert snapshot(s) == before
    saved = submit(s, original, "correct", data, "fictional-interrupted")
    assert (
        saved.version == 2
        and saved.replacement.allocation_return_ids == original.allocation_return_ids
    )


@pytest.mark.parametrize("same_key", [False, True])
def test_concurrent_commands_at_one_version_commit_once_and_replay_only_original_intent(
    drafts, same_key
):
    s = drafts
    _, _, body = setup_refund(s)
    original = refund(s, body)
    barrier = Barrier(2, timeout=20)

    def contender(index):
        barrier.wait()
        try:
            return submit(
                s,
                original,
                "correct",
                correction(s, original),
                "fictional-shared" if same_key else f"fictional-competitor-{index}",
            )
        except LedgerError as error:
            return error

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(contender, range(2)))
    if same_key:
        assert results[0] == results[1] and results[0].version == 2
    else:
        errors = [x for x in results if isinstance(x, LedgerError)]
        assert len(errors) == 1 and errors[0].code == "version_conflict"
    with s["engine"].connect() as c:
        assert (
            c.scalar(
                select(func.count(Journal.id)).where(Journal.operation_id == original.operation_id)
            )
            == 3
        )
        assert (
            c.scalar(
                select(func.count())
                .select_from(CommandReceipt)
                .where(
                    CommandReceipt.operation_id == original.operation_id,
                    CommandReceipt.response_status == 200,
                )
            )
            == 1
        )
