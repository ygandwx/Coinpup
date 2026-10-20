"""Fictional atomic credits preserve original receipts, isolated scope and closed history."""

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from decimal import Decimal
from uuid import uuid4

import pytest
from coinpup_api.business.credit_models import BusinessCredit
from coinpup_api.business.credit_schemas import BusinessCreditCreate
from coinpup_api.business.credits import BusinessCreditService
from coinpup_api.business.settlement_balances import BusinessSettlementBalances
from coinpup_api.ledger.models import JournalLine
from coinpup_api.ledger.posting_schemas import CancellationCreate
from coinpup_api.ledger.schemas import AssetUpdate, EntityCreate, EntityUpdate
from coinpup_api.ledger.service import LedgerError
from sqlalchemy import func, select

from tests.integration.business.test_confirmation_service import prepare, submit
from tests.integration.business.test_draft_service import drafts as drafts
from tests.integration.ledger.test_account_classes import snapshot
from tests.integration.ledger.test_period_transactions import change as change_period
from tests.integration.ledger.test_posting_service_database import ledger_setup as ledger_setup

pytestmark = pytest.mark.integration


def setup_credit(s, kind="invoice", count=1):
    first = s["payload"]().model_dump()["lines"][0] | dict(
        quantity="1",
        unit_price="100",
        discount_amount="0",
        tax_rate_percent="10",
        category_id=s["income"].id if kind == "invoice" else s["expenses"][0].id,
    )
    saved, command = prepare(
        s, kind=kind, changes={"lines": [first | {"id": uuid4()} for _ in range(count)]}
    )
    confirmed = submit(s, saved, command)
    request = BusinessCreditCreate(
        id=uuid4(),
        document_id=saved.id,
        expected_version=confirmed.document_version,
        credit_date="2026-10-21",
        reason="Fictional credit service 中文",
        lines=[
            dict(
                source_line_id=line.id,
                net_amount="17.23",
                tax_amount="2.77",
                recognition_date="2026-10-20",
            )
            for line in saved.lines
        ],
    )
    return saved, command, confirmed, request


def credit(s, payload, key="fictional-credit", **kwargs):
    return BusinessCreditService(s["engine"]).create(
        s["owner"], s["ledger"], payload, key, **kwargs
    )


@pytest.mark.parametrize("kind", ["invoice", "bill"])
def test_atomic_credit_reduces_gross_classification_once_and_retains_original_receipt(drafts, kind):
    s = drafts
    saved, command, original, payload = setup_credit(s, kind, count=2)
    response = credit(s, payload)
    assert len(response.postings) == 2
    assert [row.source_line_id for row in response.postings] == [row.id for row in saved.lines]
    assert all(row.total_amount == "20.000000000000000000" for row in response.postings)
    with s["engine"].connect() as connection:
        assert (
            connection.scalar(
                select(func.count())
                .select_from(JournalLine)
                .where(JournalLine.account_id == s["account"].id)
            )
            == 0
        )
        total = connection.scalar(
            select(func.sum(JournalLine.amount)).where(
                JournalLine.role == ("income" if kind == "invoice" else "expense")
            )
        )
        assert total == Decimal("-180" if kind == "invoice" else "180")
    current = BusinessSettlementBalances(s["engine"]).document_capacity(
        s["owner"], s["ledger"], saved.id
    )
    assert (current.total_amount, current.applied_amount, current.outstanding_amount) == (
        "220.00",
        "0.00",
        "180.00",
    )
    before = snapshot(s)
    assert credit(s, payload) == response
    assert submit(s, saved, command) == original
    assert snapshot(s) == before


def test_two_hundred_credit_lines_preserve_whole_group_membership(drafts):
    s = drafts
    saved, _, _, payload = setup_credit(s, count=200)
    result = credit(s, payload)
    assert [posting.source_line_id for posting in result.postings] == [
        line.id for line in saved.lines
    ]
    assert [posting.line_no for posting in result.postings] == list(range(1, 201))
    before = snapshot(s)
    assert credit(s, payload) == result
    assert snapshot(s) == before


def test_all_seven_legacy_keys_remain_reserved_for_original_intentions(legacy_v1_receipts):
    s = legacy_v1_receipts
    payload = BusinessCreditCreate(
        id=uuid4(),
        document_id=uuid4(),
        expected_version=2,
        credit_date="2026-10-21",
        reason="Fictional reserved legacy keys",
        lines=[
            dict(
                source_line_id=uuid4(),
                net_amount="1",
                tax_amount="0",
                recognition_date="2026-10-21",
            )
        ],
    )
    before = snapshot(s)
    for case in s["cases"].values():
        with pytest.raises(LedgerError) as failure:
            credit(s, payload, key=case["key"])
        assert failure.value.code == "idempotency_conflict"
    assert snapshot(s) == before


@pytest.mark.parametrize(
    "fault",
    [
        "document",
        "line",
        "version",
        "date",
        "recognition",
        "precision",
        "net",
        "tax",
        "owner",
        "ledger",
        "key",
        "asset",
    ],
)
def test_rejected_credit_rolls_back_every_table(drafts, fault):
    s = drafts
    _, _, _, payload = setup_credit(s)
    owner, ledger, key = s["owner"], s["ledger"], "fictional-credit-rejected"
    if fault == "owner":
        owner = uuid4()
    elif fault == "ledger":
        ledger = (
            s["structure"]
            .create_entity(
                s["owner"],
                EntityCreate(
                    kind="personal",
                    name="Fictional credit other ledger",
                    base_asset_id="USD",
                    template_key="personal_default",
                ),
            )
            .ledger.id
        )
    elif fault == "key":
        key = "fictional-confirm"
    elif fault == "asset":
        s["structure"].update_asset(
            s["owner"], "USD", AssetUpdate(expected_version=1, enabled=False)
        )
    elif fault == "recognition":
        change_period(s, cutoff="2026-10-20")
    elif fault in {"document", "version", "date"}:
        payload = payload.model_copy(
            update={
                "document": {"document_id": uuid4()},
                "version": {"expected_version": 1},
                "date": {"credit_date": date(2026, 10, 8)},
            }[fault]
        )
    else:
        line = payload.lines[0].model_copy(
            update={
                "line": {"source_line_id": uuid4()},
                "precision": {"net_amount": "17.230"},
                "net": {"net_amount": "100.01"},
                "tax": {"tax_amount": "10.01"},
            }[fault]
        )
        payload = payload.model_copy(update={"lines": [line]})
    before = snapshot(s)
    with pytest.raises(LedgerError):
        BusinessCreditService(s["engine"]).create(owner, ledger, payload, key)
    assert snapshot(s) == before


@pytest.mark.parametrize("same_key", [True, False])
def test_concurrent_credit_intentions_share_capacity_and_replay_once(drafts, same_key):
    s = drafts
    _, _, _, payload = setup_credit(s)
    payload = payload.model_copy(
        update={
            "lines": [payload.lines[0].model_copy(update={"net_amount": "60", "tax_amount": "0"})]
        }
    )

    def attempt(index):
        try:
            return credit(
                s,
                payload if same_key else payload.model_copy(update={"id": uuid4()}),
                "fictional-same" if same_key else f"fictional-{index}",
            )
        except LedgerError as error:
            return error

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(attempt, range(2)))
    if same_key:
        assert not isinstance(results[0], Exception) and results[0] == results[1]
    else:
        failures = [row for row in results if isinstance(row, LedgerError)]
        assert len(failures) == 1 and failures[0].code == "credit_exceeds_source"
    with s["engine"].connect() as connection:
        assert connection.scalar(select(func.count()).select_from(BusinessCredit)) == 1


def test_raw_intent_spelling_is_permanent_after_period_asset_and_entity_closure(drafts):
    s = drafts
    _, _, _, payload = setup_credit(s)
    raw = json.dumps(payload.model_dump(mode="json"), ensure_ascii=False).encode()
    result = credit(s, payload, raw_body=raw)
    change_period(s, cutoff="2026-10-21")
    s["structure"].update_asset(s["owner"], "USD", AssetUpdate(expected_version=1, enabled=False))
    s["structure"].update_entity(
        s["owner"], s["entity"].id, EntityUpdate(expected_version=1, archived=True)
    )
    before = snapshot(s)
    assert credit(s, payload, raw_body=raw) == result
    altered = json.loads(raw)
    altered["lines"][0]["net_amount"] = "17.230"
    with pytest.raises(LedgerError) as failure:
        credit(
            s, BusinessCreditCreate.model_validate(altered), raw_body=json.dumps(altered).encode()
        )
    assert failure.value.code == "idempotency_conflict"
    assert snapshot(s) == before


def test_second_group_write_failure_rolls_back_first_group_and_notifications(drafts, monkeypatch):
    s = drafts
    _, _, _, payload = setup_credit(s, count=2)
    from coinpup_api.business import credits

    real = credits.persist_credit

    def fail(session, owner, ledger, body, key, groups, now):
        real(session, owner, ledger, body, key, groups[:1], now)
        raise RuntimeError("Fictional interruption after one persisted line")

    monkeypatch.setattr(credits, "persist_credit", fail)
    before = snapshot(s)
    with pytest.raises(RuntimeError, match="Fictional interruption"):
        credit(s, payload)
    assert snapshot(s) == before


def test_credit_operations_cannot_use_old_single_operation_cancellation(drafts):
    s = drafts
    _, _, _, payload = setup_credit(s)
    result = credit(s, payload)
    before = snapshot(s)
    for posting in result.postings:
        with pytest.raises(LedgerError) as failure:
            s["posting"].cancel_operation(
                s["owner"],
                s["ledger"],
                posting.operation_id,
                CancellationCreate(expected_version=1, reason="Fictional invalid old path"),
                "fictional-old-credit-cancel",
            )
        assert failure.value.status == 404
    assert snapshot(s) == before
