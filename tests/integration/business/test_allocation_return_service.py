"""Fictional atomic returns, original-intent replay and fresh concurrent capacity."""

import json
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest
from coinpup_api.business.allocation_return_models import (
    BusinessAllocationReturn,
    BusinessAllocationReturnLine,
)
from coinpup_api.business.allocation_return_schemas import BusinessAllocationReturnCreate
from coinpup_api.business.allocation_returns import BusinessAllocationReturnService
from coinpup_api.business.schemas import PartyUpdate, ProjectUpdate
from coinpup_api.business.settlement_models import BusinessAllocationLine
from coinpup_api.business.settlement_reads import BusinessSettlementReads
from coinpup_api.ledger.models import CommandReceipt, Journal, JournalLine
from coinpup_api.ledger.schemas import AccountUpdate, AssetUpdate, EntityCreate, EntityUpdate
from coinpup_api.ledger.service import LedgerError
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from tests.integration.business.test_allocation_return_storage import rows
from tests.integration.business.test_draft_service import drafts as drafts
from tests.integration.business.test_payment_activation import balance
from tests.integration.business.test_settlement_service import allocate, pay, prepared
from tests.integration.ledger.test_account_classes import snapshot
from tests.integration.ledger.test_period_transactions import change as change_period
from tests.integration.ledger.test_posting_service_database import ledger_setup as ledger_setup

pytestmark = pytest.mark.integration


def setup_return(s, *, direction="payout", asset="USD", count=1, full=False):
    payment, allocation = prepared(s, direction=direction, asset=asset, count=count)
    paid = pay(s, payment)
    with s["engine"].connect() as c:
        identifiers = c.scalars(
            select(BusinessAllocationLine.id)
            .where(BusinessAllocationLine.allocation_id == allocation.id)
            .order_by(BusinessAllocationLine.line_no)
        ).all()
    amounts = (
        ("110" if asset == "EUR" else "100", "100")
        if full
        else ("22" if asset == "EUR" else "20", "20")
    )
    payload = BusinessAllocationReturnCreate(
        id=uuid4(),
        payment_id=payment.id,
        return_date="2026-10-01",
        reason="Fictional partial return 中文",
        lines=[
            dict(
                source_allocation_line_id=identifier,
                expected_version=1,
                payment_amount=amounts[0],
                document_amount=amounts[1],
            )
            for identifier in identifiers
        ],
    )
    return payment, paid, allocation, payload


def submit_return(s, payload, key="fictional-return", **kwargs):
    return BusinessAllocationReturnService(s["engine"]).create(
        s["owner"], s["ledger"], payload, key, **kwargs
    )


def test_final_return_absorbs_the_exact_remaining_minimum_unit(drafts):
    s = drafts
    values = rows(
        s,
        document_asset="EUR",
        source_payment_amount="0.03",
        source_document_amount="0.04",
        return_payment_amount="0.01",
        return_document_amount="0.01",
    )
    payload = BusinessAllocationReturnCreate(
        id=uuid4(),
        payment_id=values["facts"][0].id,
        return_date="2026-10-01",
        reason="Fictional rounding remainder",
        lines=[
            dict(
                source_allocation_line_id=values["member"]["source_allocation_line_id"],
                expected_version=1,
                payment_amount="0.01",
                document_amount="0.01",
            )
        ],
    )
    for index in range(2):
        submit_return(s, payload.model_copy(update={"id": uuid4()}), f"fictional-cent-{index}")
    before = snapshot(s)
    with pytest.raises(LedgerError) as error:
        submit_return(s, payload)
    assert error.value.code == "allocation_return_residual"
    assert snapshot(s) == before
    final = payload.model_copy(
        update={"lines": [payload.lines[0].model_copy(update={"document_amount": "0.02"})]}
    )
    result = submit_return(s, final)
    assert result.postings[0].payment_amount == "0.010000000000000000"
    assert result.postings[0].document_amount == "0.020000000000000000"


@pytest.mark.parametrize("direction", ["receipt", "payout"])
@pytest.mark.parametrize("asset", ["USD", "EUR"])
def test_return_then_residual_and_reapplication_keep_original_facts(drafts, direction, asset):
    s = drafts
    payment, paid, allocation, body = setup_return(s, direction=direction, asset=asset)
    with s["engine"].connect() as c:
        cash_before = balance(c, s["account"].id, "USD")
        categories_before = c.execute(
            select(JournalLine.category_id, func.sum(JournalLine.amount))
            .where(JournalLine.category_id.is_not(None))
            .group_by(JournalLine.category_id)
        ).all()
    result = submit_return(s, body)
    assert result.payment_id == payment.id
    assert result.postings[0].source_allocation_line_id == body.lines[0].source_allocation_line_id
    assert result.postings[0].payment_amount == (
        "22.000000000000000000" if asset == "EUR" else "20.000000000000000000"
    )
    reads = BusinessSettlementReads(s["engine"])
    assert reads.get_payment(s["owner"], s["ledger"], payment.id).receipt == paid
    before = snapshot(s)
    assert submit_return(s, body) == result
    assert snapshot(s) == before
    residual = body.model_copy(
        update={
            "id": uuid4(),
            "lines": [
                body.lines[0].model_copy(
                    update={
                        "payment_amount": "88" if asset == "EUR" else "80",
                        "document_amount": "80",
                    }
                )
            ],
        }
    )
    submit_return(s, residual, "fictional-residual")
    allocation = allocation.model_copy(update={"id": uuid4()})
    allocate(s, allocation, "fictional-reapply")
    assert reads.get_payment(s["owner"], s["ledger"], payment.id).unapplied_amount == "0.00"
    before = snapshot(s)
    assert submit_return(s, body) == result
    assert pay(s, payment) == paid
    assert snapshot(s) == before
    with s["engine"].connect() as c:
        assert cash_before == balance(c, s["account"].id, "USD")
        assert (
            categories_before
            == c.execute(
                select(JournalLine.category_id, func.sum(JournalLine.amount))
                .where(JournalLine.category_id.is_not(None))
                .group_by(JournalLine.category_id)
            ).all()
        )
        assert c.scalar(select(func.count()).select_from(BusinessAllocationReturn)) == 2
        assert c.scalars(
            select(BusinessAllocationReturnLine.source_return_no).order_by(
                BusinessAllocationReturnLine.source_return_no
            )
        ).all() == [1, 2]
        assert all(
            amount == Decimal(0)
            for amount in c.scalars(
                select(func.sum(JournalLine.amount)).group_by(JournalLine.asset_id)
            )
        )


@pytest.mark.parametrize("same_key", [True, False])
def test_concurrent_returns_share_one_receipt_or_reject_exhausted_capacity(drafts, same_key):
    s = drafts
    _, _, _, body = setup_return(s, full=True)

    def attempt(index):
        payload = body if same_key else body.model_copy(update={"id": uuid4()})
        try:
            return submit_return(
                s, payload, "fictional-concurrent" + ("" if same_key else str(index))
            )
        except LedgerError as error:
            return error

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(attempt, range(2)))
    if same_key:
        assert results[0] == results[1]
    else:
        errors = [result for result in results if isinstance(result, LedgerError)]
        assert len(errors) == 1 and errors[0].code == "allocation_return_exceeds_source"
    with s["engine"].connect() as c:
        assert c.scalar(select(func.count()).select_from(BusinessAllocationReturn)) == 1


@pytest.mark.parametrize(
    "fault",
    [
        "source",
        "payment",
        "version",
        "date",
        "ratio",
        "precision",
        "capacity",
        "period",
        "owner",
        "ledger",
        "key",
    ],
)
def test_rejected_intents_roll_back_every_table(drafts, fault):
    s = drafts
    _, _, _, body = setup_return(s)
    scope, owner, key = s["ledger"], s["owner"], "fictional-invalid"
    if fault == "ledger":
        scope = (
            s["structure"]
            .create_entity(s["owner"], EntityCreate(kind="personal", name="Fictional other ledger"))
            .ledger.id
        )
    elif fault == "owner":
        owner = uuid4()
    elif fault == "key":
        key = "fictional-pay"  # Existing payment receipt must reserve this namespace forever.
    elif fault == "period":
        change_period(s, cutoff="2026-10-01")
    elif fault == "payment":
        body = body.model_copy(update={"payment_id": uuid4()})
    elif fault == "date":
        body = BusinessAllocationReturnCreate.model_validate(
            body.model_dump() | {"return_date": "2026-09-29"}
        )
    else:
        changes = {
            "source": {"source_allocation_line_id": uuid4()},
            "version": {"expected_version": 2},
            "ratio": {"payment_amount": "19"},
            "precision": {"payment_amount": "20.001"},
            "capacity": {"payment_amount": "101", "document_amount": "101"},
        }[fault]
        body = body.model_copy(update={"lines": [body.lines[0].model_copy(update=changes)]})
    before = snapshot(s)
    with pytest.raises(LedgerError):
        BusinessAllocationReturnService(s["engine"]).create(owner, scope, body, key)
    assert snapshot(s) == before


def test_archived_sources_can_return_and_original_raw_intent_replays_after_disable(drafts):
    s = drafts
    _, _, _, body = setup_return(s)
    s["master"].update_party(
        s["owner"], s["ledger"], s["party"].id, PartyUpdate(expected_version=1, archived=True)
    )
    s["master"].update_project(
        s["owner"], s["ledger"], s["project"].id, ProjectUpdate(expected_version=1, archived=True)
    )
    s["structure"].update_account(
        s["owner"], s["ledger"], s["account"].id, AccountUpdate(expected_version=1, archived=True)
    )
    raw = json.dumps(body.model_dump(mode="json"), ensure_ascii=False, indent=2).encode()
    result = submit_return(s, body, raw_body=raw)
    change_period(s, cutoff="2026-10-01")
    s["structure"].update_asset(s["owner"], "USD", AssetUpdate(expected_version=1, enabled=False))
    s["structure"].update_entity(
        s["owner"], s["entity"].id, EntityUpdate(expected_version=1, archived=True)
    )
    before = snapshot(s)
    assert submit_return(s, body, raw_body=raw) == result
    changed = body.model_copy(
        update={"lines": [body.lines[0].model_copy(update={"payment_amount": "20.00"})]}
    )
    with pytest.raises(LedgerError) as error:
        submit_return(s, changed)
    assert error.value.code == "idempotency_conflict"
    assert snapshot(s) == before


@pytest.mark.parametrize("point", ["second_journal", "member", "receipt"])
def test_mid_command_failure_rolls_back_journals_members_receipt_and_notifications(
    drafts, monkeypatch, point
):
    s = drafts
    _, _, _, body = setup_return(s, count=2)
    before = snapshot(s)
    execute, seen = Session.execute, 0

    def fail(self, statement, *args, **kwargs):
        nonlocal seen
        table = getattr(getattr(statement, "table", None), "name", None)
        if table == Journal.__tablename__:
            seen += 1
        if (
            (point == "second_journal" and table == Journal.__tablename__ and seen == 2)
            or (point == "member" and table == BusinessAllocationReturnLine.__tablename__)
            or (point == "receipt" and table == CommandReceipt.__tablename__)
        ):
            raise RuntimeError("Fictional return failure")
        return execute(self, statement, *args, **kwargs)

    monkeypatch.setattr(Session, "execute", fail)
    with pytest.raises(RuntimeError, match="Fictional return failure"):
        submit_return(s, body)
    assert snapshot(s) == before


@pytest.mark.parametrize("fault", ["amount", "anchor", "status"])
def test_replay_rejects_receipt_corruption_without_writing(drafts, monkeypatch, fault):
    s = drafts
    _, _, _, body = setup_return(s)
    submit_return(s, body)
    original = Session.get

    def corrupt(self, entity, *args, **kwargs):
        row = original(self, entity, *args, **kwargs)
        if entity == CommandReceipt and row is not None and row.key == "fictional-return":
            values = {
                name: getattr(row, name)
                for name in (
                    "key",
                    "hash_version",
                    "request_hash",
                    "response",
                    "response_status",
                    "operation_id",
                )
            }
            values["response"] = json.loads(json.dumps(row.response))
            if fault == "amount":
                values["response"]["postings"][0]["payment_amount"] = "19.000000000000000000"
            elif fault == "anchor":
                values["operation_id"] = uuid4()
            else:
                values["response_status"] = 200
            return SimpleNamespace(**values)
        return row

    before = snapshot(s)
    monkeypatch.setattr(Session, "get", corrupt)
    with pytest.raises(LedgerError) as error:
        submit_return(s, body)
    assert error.value.code == "allocation_return_integrity"
    assert snapshot(s) == before
