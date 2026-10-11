"""Fictional atomic settlement commands, original intent and whole-group recovery."""

import json
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from uuid import uuid4

import pytest
from coinpup_api.business.schemas import PartyUpdate, ProjectUpdate
from coinpup_api.business.settlement_models import BusinessAllocationLine, BusinessPayment
from coinpup_api.business.settlement_reads import BusinessSettlementReads
from coinpup_api.business.settlement_schemas import BusinessAllocationCreate, BusinessPaymentCreate
from coinpup_api.business.settlements import BusinessSettlementService
from coinpup_api.ledger.models import Account, CommandReceipt, Journal
from coinpup_api.ledger.schemas import (
    AccountUpdate,
    AssetUpdate,
    CategoryUpdate,
    EntityCreate,
    EntityUpdate,
)
from coinpup_api.ledger.service import LedgerError
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from tests.integration.business.test_confirmation_service import prepare, submit
from tests.integration.business.test_draft_service import drafts as drafts
from tests.integration.business.test_payment_activation import balance
from tests.integration.ledger.test_account_classes import snapshot
from tests.integration.ledger.test_period_transactions import change as change_period
from tests.integration.ledger.test_posting_service_database import ledger_setup as ledger_setup
from tests.integration.ledger.test_posting_service_database import opening

pytestmark = pytest.mark.integration


def prepared(s, *, direction="payout", asset="USD", initial=True, count=1):
    s["posting"].post_opening(s["owner"], s["ledger"], opening(s, "1000", "USD"), "fictional-seed")
    line = s["payload"]().model_dump()["lines"][0]
    lines = [
        line
        | dict(
            id=uuid4(),
            quantity="1",
            unit_price="100",
            discount_amount="0",
            tax_rate_percent="0",
            category_id=s["income"].id if direction == "receipt" else s["expenses"][0].id,
        )
        for _ in range(count)
    ]
    document, request = prepare(
        s,
        kind="invoice" if direction == "receipt" else "bill",
        changes=dict(asset_id=asset, issue_date="2026-09-30", lines=lines),
    )
    submit(s, document, request)
    group = dict(
        id=uuid4(),
        allocation_date="2026-09-30",
        lines=[
            dict(
                document_id=document.id,
                document_line_id=row.id,
                payment_amount=row.total_amount if asset == "USD" else "110",
                document_amount=row.total_amount,
            )
            for row in document.lines
        ],
    )
    principal = str((100 if asset == "USD" else 110) * count)
    payload = BusinessPaymentCreate(
        id=uuid4(),
        direction=direction,
        party_id=s["party"].id,
        account_id=s["account"].id,
        asset_id="USD",
        amount=principal,
        transaction_date="2026-09-15",
        description="Fictional payment service",
        fees=[
            dict(
                account_id=s["account"].id,
                asset_id="USD",
                amount="1",
                category_id=s["expenses"][0].id,
            )
        ],
        initial_allocation=group if initial else None,
    )
    return payload, BusinessAllocationCreate(payment_id=payload.id, **group)


def pay(s, payload, key="fictional-pay", **kwargs):
    return BusinessSettlementService(s["engine"]).pay(
        s["owner"], s["ledger"], payload, key, **kwargs
    )


def allocate(s, payload, key="fictional-allocate", **kwargs):
    return BusinessSettlementService(s["engine"]).allocate(
        s["owner"], s["ledger"], payload, key, **kwargs
    )


@pytest.mark.parametrize("direction", ["receipt", "payout"])
@pytest.mark.parametrize("asset", ["USD", "EUR"])
@pytest.mark.parametrize("initial", [True, False])
def test_exact_atomic_payment_and_later_allocation_replay(drafts, direction, asset, initial):
    s = drafts
    body, group = prepared(s, direction=direction, asset=asset, initial=initial)
    result = pay(s, body)
    applied = result.initial_allocation if initial else allocate(s, group)
    before = snapshot(s)
    assert pay(s, body) == result
    if not initial:
        assert allocate(s, group) == applied
    assert snapshot(s) == before
    with s["engine"].connect() as c:
        expected = 1000 + (int(body.amount) if direction == "receipt" else -int(body.amount)) - 1
        assert balance(c, s["account"].id, "USD") == Decimal(expected)
        assert c.scalar(select(func.count()).select_from(BusinessPayment)) == 1
        assert c.scalar(select(func.sum(BusinessAllocationLine.payment_amount))) == Decimal(
            body.amount
        )


def test_two_hundred_members_are_complete_and_replay_without_writes(drafts):
    s = drafts
    body, _ = prepared(s, count=200)
    result = pay(s, body)
    assert len(result.initial_allocation.postings) == 200
    before = snapshot(s)
    assert pay(s, body) == result
    reads = BusinessSettlementReads(s["engine"])
    assert reads.get_payment(s["owner"], s["ledger"], body.id).receipt == result
    detail = reads.get_allocation(s["owner"], s["ledger"], result.initial_allocation.id)
    assert len(detail.lines) == 200 and detail.receipt == result
    assert snapshot(s) == before


@pytest.mark.parametrize("initial", [True, False])
def test_concurrent_same_key_commits_one_complete_original_receipt(drafts, initial):
    s = drafts
    body, group = prepared(s, initial=initial)
    if not initial:
        pay(s, body)
    action, payload = (pay, body) if initial else (allocate, group)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: action(s, payload), range(2)))
    assert results[0] == results[1]
    with s["engine"].connect() as c:
        assert c.scalar(select(func.count()).select_from(BusinessAllocationLine)) == 1


def test_original_raw_intent_replays_after_archive_disable_and_close(drafts):
    s = drafts
    body, _ = prepared(s)
    raw = json.dumps(body.model_dump(mode="json"), indent=2).encode()
    result = pay(s, body, raw_body=raw)
    s["master"].update_party(
        s["owner"], s["ledger"], s["party"].id, PartyUpdate(expected_version=1, archived=True)
    )
    s["master"].update_project(
        s["owner"], s["ledger"], s["project"].id, ProjectUpdate(expected_version=1, archived=True)
    )
    s["structure"].update_category(
        s["owner"],
        s["ledger"],
        s["expenses"][0].id,
        CategoryUpdate(expected_version=1, archived=True),
    )
    change_period(s, cutoff="2026-09-30")
    s["structure"].update_asset(s["owner"], "USD", AssetUpdate(expected_version=1, enabled=False))
    s["structure"].update_entity(
        s["owner"], s["entity"].id, EntityUpdate(expected_version=1, archived=True)
    )
    before = snapshot(s)
    assert pay(s, body, raw_body=raw) == result
    changed = body.model_copy(update={"amount": body.amount + ".0"})
    with pytest.raises(LedgerError) as error:
        pay(s, changed)
    assert error.value.code == "idempotency_conflict"
    assert snapshot(s) == before


@pytest.mark.parametrize("point", ["second_journal", "receipt"])
def test_failure_rolls_back_advance_creation_payment_allocations_and_notifications(
    drafts, monkeypatch, point
):
    s = drafts
    body, _ = prepared(s)
    before = snapshot(s)
    execute, seen = Session.execute, 0

    def fail(self, statement, *args, **kwargs):
        nonlocal seen
        table = getattr(getattr(statement, "table", None), "name", None)
        if table == Journal.__tablename__:
            seen += 1
        if (point == "second_journal" and table == Journal.__tablename__ and seen == 2) or (
            point == "receipt" and table == CommandReceipt.__tablename__
        ):
            raise RuntimeError("Fictional settlement failure")
        return execute(self, statement, *args, **kwargs)

    monkeypatch.setattr(Session, "execute", fail)
    with pytest.raises(RuntimeError, match="Fictional settlement failure"):
        pay(s, body)
    assert snapshot(s) == before
    with s["engine"].connect() as c:
        assert c.scalar(select(Account.id).where(Account.system_key == "advance.paid")) is None


@pytest.mark.parametrize(
    "fault", ["party", "account", "source", "date", "capacity", "period", "owner"]
)
def test_invalid_new_intent_leaves_every_table_unchanged(drafts, fault):
    s = drafts
    body, _ = prepared(s)
    changes = {}
    if fault in {"party", "account"}:
        changes[fault + "_id"] = uuid4()
    elif fault == "source":
        changes["initial_allocation"] = body.initial_allocation.model_copy(
            update={
                "lines": [
                    body.initial_allocation.lines[0].model_copy(update={"document_id": uuid4()})
                ]
            }
        )
    elif fault == "date":
        changes["initial_allocation"] = body.initial_allocation.model_copy(
            update={"allocation_date": body.transaction_date}
        )
    elif fault == "capacity":
        changes["amount"] = "1"
    elif fault == "period":
        change_period(s, cutoff="2026-09-30")
    before = snapshot(s)
    with pytest.raises(LedgerError):
        if fault == "owner":
            BusinessSettlementService(s["engine"]).pay(uuid4(), s["ledger"], body, "fictional-pay")
        else:
            pay(s, body.model_copy(update=changes))
    assert snapshot(s) == before


def test_old_v1_command_keys_remain_reserved_before_new_references(legacy_v1_receipts):
    s = legacy_v1_receipts
    body = BusinessPaymentCreate(
        id=uuid4(),
        direction="payout",
        party_id=uuid4(),
        account_id=uuid4(),
        asset_id="USD",
        amount="1",
        transaction_date="2026-09-30",
    )
    before = snapshot(s)
    for case in s["cases"].values():
        with pytest.raises(LedgerError) as error:
            pay(s, body, case["key"])
        assert error.value.code == "idempotency_conflict"
    assert snapshot(s) == before


def test_new_allocation_uses_sealed_references_after_archival(drafts):
    s = drafts
    body, group = prepared(s, initial=False)
    paid = pay(s, body)
    s["master"].update_party(
        s["owner"], s["ledger"], s["party"].id, PartyUpdate(expected_version=1, archived=True)
    )
    s["master"].update_project(
        s["owner"], s["ledger"], s["project"].id, ProjectUpdate(expected_version=1, archived=True)
    )
    s["structure"].update_category(
        s["owner"],
        s["ledger"],
        s["expenses"][0].id,
        CategoryUpdate(expected_version=1, archived=True),
    )
    s["structure"].update_account(
        s["owner"], s["ledger"], s["account"].id, AccountUpdate(expected_version=1, archived=True)
    )
    result = allocate(s, group)
    before = snapshot(s)
    assert allocate(s, group) == result and pay(s, body) == paid
    assert snapshot(s) == before


def test_commands_do_not_inherit_an_unsafe_snapshot_default(drafts):
    s = drafts
    body, _ = prepared(s)
    service = BusinessSettlementService(
        s["engine"].execution_options(isolation_level="REPEATABLE READ")
    )
    assert (
        service.pay(s["owner"], s["ledger"], body, "fictional-pay").initial_allocation is not None
    )


def test_existing_payment_and_sources_cannot_cross_owned_ledgers(drafts):
    s = drafts
    body, group = prepared(s, initial=False)
    pay(s, body)
    other = s["structure"].create_entity(
        s["owner"],
        EntityCreate(
            kind="personal",
            name="Fictional other settlement",
            base_asset_id="USD",
            template_key="personal_default",
        ),
    )
    service = BusinessSettlementService(s["engine"])
    before = snapshot(s)
    for method, payload in ((service.pay, body), (service.allocate, group)):
        with pytest.raises(LedgerError) as error:
            method(s["owner"], other.ledger.id, payload, "fictional-pay")
        assert error.value.code == "not_found"
        assert snapshot(s) == before


def test_multiple_payments_use_exact_remaining_source_and_payment_capacity(drafts):
    s = drafts
    body, group = prepared(s, initial=False)
    pay(s, body)
    second = body.model_copy(update={"id": uuid4()})
    pay(s, second, "fictional-second-payment")
    for index, payment_id, quantity in ((1, body.id, "40"), (2, second.id, "60")):
        portion = group.model_copy(
            update={
                "id": uuid4(),
                "payment_id": payment_id,
                "lines": [
                    group.lines[0].model_copy(
                        update={"payment_amount": quantity, "document_amount": quantity}
                    )
                ],
            }
        )
        allocate(s, portion, "fictional-portion-" + str(index))
    before = snapshot(s)
    extra = group.model_copy(
        update={
            "id": uuid4(),
            "lines": [
                group.lines[0].model_copy(
                    update={"payment_amount": "0.01", "document_amount": "0.01"}
                )
            ],
        }
    )
    with pytest.raises(LedgerError) as error:
        allocate(s, extra, "fictional-excess")
    assert error.value.code == "allocation_exceeds_document"
    assert snapshot(s) == before
