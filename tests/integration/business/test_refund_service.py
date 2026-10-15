"""Fictional actual cash refunds preserve independent classification and original receipts."""

import json
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from uuid import uuid4

import pytest
from coinpup_api.business.refund_models import BusinessRefund
from coinpup_api.business.refund_schemas import BusinessRefundCreate
from coinpup_api.business.refunds import BusinessRefundService
from coinpup_api.business.schemas import PartyUpdate
from coinpup_api.business.settlement_reads import BusinessSettlementReads
from coinpup_api.ledger.models import JournalLine
from coinpup_api.ledger.schemas import AccountUpdate, AssetUpdate, EntityCreate, EntityUpdate
from coinpup_api.ledger.service import LedgerError
from sqlalchemy import func, select

from tests.integration.business.test_allocation_return_service import setup_return, submit_return
from tests.integration.business.test_draft_service import drafts as drafts
from tests.integration.business.test_payment_activation import balance
from tests.integration.business.test_settlement_service import pay, prepared
from tests.integration.ledger.test_account_classes import snapshot
from tests.integration.ledger.test_period_transactions import change as change_period
from tests.integration.ledger.test_posting_service_database import ledger_setup as ledger_setup
from tests.integration.ledger.test_posting_service_database import opening

pytestmark = pytest.mark.integration


def setup_refund(s, direction="payout", asset="USD", amount="20", *, allocated=True):
    if allocated:
        payment, paid, _, returned = setup_return(s, direction=direction)
        identifiers = [submit_return(s, returned).id]
    else:
        payment, _ = prepared(s, direction=direction, initial=False)
        paid, identifiers = pay(s, payment), []
    if asset != "USD":
        s["posting"].post_opening(
            s["owner"], s["ledger"], opening(s, "1000", asset), "fictional-other-cash"
        )
    return (
        payment,
        paid,
        BusinessRefundCreate(
            id=uuid4(),
            payment_id=payment.id,
            expected_version=1,
            account_id=payment.account_id,
            asset_id=asset,
            amount=amount,
            payment_amount="20",
            transaction_date="2026-10-02",
            reason="Fictional actual refund 中文",
            allocation_return_ids=identifiers,
        ),
    )


def refund(s, payload, key="fictional-refund", **kwargs):
    return BusinessRefundService(s["engine"]).create(
        s["owner"], s["ledger"], payload, key, **kwargs
    )


def category_totals(connection):
    return connection.execute(
        select(JournalLine.category_id, JournalLine.asset_id, func.sum(JournalLine.amount))
        .where(JournalLine.category_id.is_not(None))
        .group_by(JournalLine.category_id, JournalLine.asset_id)
        .order_by(JournalLine.category_id, JournalLine.asset_id)
    ).all()


@pytest.mark.parametrize("direction", ["receipt", "payout"])
@pytest.mark.parametrize("asset,amount", [("USD", "20"), ("EUR", "19.37"), ("BTC", "0.00000001")])
def test_actual_cash_changes_once_but_classification_and_old_receipt_do_not(
    drafts, direction, asset, amount
):
    s = drafts
    payment, paid, body = setup_refund(s, direction, asset, amount)
    with s["engine"].connect() as c:
        cash_before = balance(c, payment.account_id, asset)
        categories = category_totals(c)
    result = refund(s, body)
    assert result.payment_id == payment.id
    assert result.payment_asset_id == "USD" and Decimal(result.payment_amount) == 20
    with s["engine"].connect() as c:
        assert balance(c, payment.account_id, asset) == cash_before + (
            Decimal(amount) if direction == "payout" else -Decimal(amount)
        )
        assert category_totals(c) == categories
        totals = c.execute(
            select(JournalLine.asset_id, func.sum(JournalLine.amount))
            .where(JournalLine.journal_id == result.journal_id)
            .group_by(JournalLine.asset_id)
        ).all()
        assert len(totals) == (1 if asset == "USD" else 2)
        assert all(total == 0 for _, total in totals)
    detail = BusinessSettlementReads(s["engine"]).get_payment(s["owner"], s["ledger"], payment.id)
    assert detail.receipt == paid and detail.applied_amount == "80.00"
    assert detail.unapplied_amount == "0.00"
    before = snapshot(s)
    assert refund(s, body) == result
    assert pay(s, payment) == paid
    assert snapshot(s) == before


def test_unapplied_advance_can_refund_in_open_period_with_independent_fees(drafts):
    s = drafts
    payment, _, body = setup_refund(s, allocated=False)
    change_period(s, cutoff="2026-10-01")
    body = BusinessRefundCreate.model_validate(
        body.model_dump()
        | dict(
            amount="100",
            payment_amount="100",
            fees=[
                dict(
                    account_id=payment.account_id,
                    asset_id="USD",
                    amount="0.25",
                    category_id=s["expenses"][0].id,
                )
            ],
        )
    )
    with s["engine"].connect() as c:
        before_cash = balance(c, payment.account_id, "USD")
    result = refund(s, body)
    assert result.allocation_return_ids == () and len(result.fees) == 1
    with s["engine"].connect() as c:
        assert balance(c, payment.account_id, "USD") == before_cash + Decimal("99.75")
        assert c.scalar(
            select(func.sum(JournalLine.amount)).where(
                JournalLine.journal_id == result.journal_id, JournalLine.role == "expense"
            )
        ) == Decimal("0.25")


@pytest.mark.parametrize("same_key", [False, True])
def test_concurrent_refunds_serialize_fresh_capacity_and_original_intent(drafts, same_key):
    s = drafts
    _, _, body = setup_refund(s)

    def execute(index):
        try:
            return refund(
                s,
                body if same_key else body.model_copy(update={"id": uuid4()}),
                "fictional-same" if same_key else f"fictional-{index}",
            )
        except LedgerError as error:
            return error

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(execute, range(2)))
    if same_key:
        assert results[0] == results[1]
    else:
        errors = [r for r in results if isinstance(r, LedgerError)]
        assert len(errors) == 1 and errors[0].code == "refund_exceeds_payment"
    with s["engine"].connect() as c:
        assert c.scalar(select(func.count()).select_from(BusinessRefund)) == 1


@pytest.mark.parametrize(
    "fault",
    [
        "payment",
        "return",
        "version",
        "date",
        "precision",
        "capacity",
        "account",
        "asset",
        "period",
        "owner",
        "ledger",
        "key",
        "fee",
        "cash",
    ],
)
def test_rejected_refunds_preserve_all_tables(drafts, fault):
    s = drafts
    _, _, body = setup_refund(s)
    scope, owner, key = s["ledger"], s["owner"], "fictional-rejected"
    if fault == "owner":
        owner = uuid4()
    elif fault == "ledger":
        scope = (
            s["structure"]
            .create_entity(
                s["owner"],
                EntityCreate(
                    kind="personal",
                    name="Fictional isolated refund",
                    base_asset_id="USD",
                    template_key="personal_default",
                ),
            )
            .ledger.id
        )
    elif fault == "key":
        key = "fictional-pay"
    elif fault == "period":
        change_period(s, cutoff="2026-10-02")
    else:
        changes = {
            "payment": {"payment_id": uuid4()},
            "return": {"allocation_return_ids": [uuid4()]},
            "version": {"expected_version": 2},
            "date": {"transaction_date": "2026-09-30"},
            "precision": {"amount": "20.001"},
            "capacity": {"amount": "20.01", "payment_amount": "20.01"},
            "account": {"account_id": uuid4()},
            "asset": {"asset_id": "UNKNOWN"},
            "cash": {"asset_id": "EUR", "amount": "99999999999999999999.99"},
            "fee": {
                "fees": [
                    dict(
                        account_id=body.account_id,
                        asset_id="USD",
                        amount="1",
                        category_id=s["income"].id,
                    )
                ]
            },
        }[fault]
        # Receiving the maximum amount into an already positive account exceeds storage range.
        if fault == "cash":
            s["posting"].post_opening(
                s["owner"], s["ledger"], opening(s, "1000", "EUR"), "fictional-range-seed"
            )
        body = BusinessRefundCreate.model_validate(body.model_dump() | changes)
    before = snapshot(s)
    with pytest.raises(LedgerError):
        BusinessRefundService(s["engine"]).create(owner, scope, body, key)
    assert snapshot(s) == before


def test_archived_party_is_historical_but_new_cash_must_be_active_and_replay_survives(drafts):
    s = drafts
    _, _, body = setup_refund(s)
    s["master"].update_party(
        s["owner"], s["ledger"], s["party"].id, PartyUpdate(expected_version=1, archived=True)
    )
    raw = json.dumps(body.model_dump(mode="json"), ensure_ascii=False).encode()
    result = refund(s, body, raw_body=raw)
    change_period(s, cutoff="2026-10-02")
    s["structure"].update_account(
        s["owner"], s["ledger"], s["account"].id, AccountUpdate(expected_version=1, archived=True)
    )
    s["structure"].update_asset(s["owner"], "USD", AssetUpdate(expected_version=1, enabled=False))
    s["structure"].update_entity(
        s["owner"], s["entity"].id, EntityUpdate(expected_version=1, archived=True)
    )
    before = snapshot(s)
    assert refund(s, body, raw_body=raw) == result
    with pytest.raises(LedgerError) as error:
        refund(s, body.model_copy(update={"amount": "20.00"}))
    assert error.value.code == "idempotency_conflict"
    assert snapshot(s) == before


def test_failure_after_posting_rolls_back_refund_references_journal_and_receipt(
    drafts, monkeypatch
):
    s = drafts
    _, _, body = setup_refund(s)
    before = snapshot(s)

    def fail(*args):
        raise RuntimeError("fictional post-persistence failure")

    monkeypatch.setattr("coinpup_api.business.refunds.response_from_rows", fail)
    with pytest.raises(RuntimeError, match="fictional post-persistence"):
        refund(s, body)
    assert snapshot(s) == before
