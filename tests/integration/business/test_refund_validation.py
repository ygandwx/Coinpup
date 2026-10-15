"""Direct fictional database writes must prove refund shape, capacity and immutable receipt."""

from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from coinpup_api.business.refund_models import BusinessRefund, BusinessRefundReturn
from coinpup_api.business.refund_plan import RefundPlan
from coinpup_api.business.refund_rows import refund_rows
from coinpup_api.business.refund_schemas import BusinessRefundCreate, BusinessRefundResponse
from coinpup_api.business.settlement_rows import persist_posting
from coinpup_api.business.settlement_schemas import BusinessAllocationCreate
from coinpup_api.business.settlements import BusinessSettlementService
from coinpup_api.ledger.assets import ASSETS
from coinpup_api.ledger.control_accounts import ControlAccounts
from coinpup_api.ledger.models import CommandReceipt
from coinpup_api.ledger.money import Amount
from coinpup_api.ledger.service import LedgerError
from coinpup_api.sync.locking import acquire_write_lock
from coinpup_api.sync.service import ChangeService
from sqlalchemy import func, insert, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from tests.integration.business.test_allocation_return_service import setup_return, submit_return
from tests.integration.business.test_draft_service import drafts as drafts
from tests.integration.conftest import refund_validation_migration
from tests.integration.ledger.test_account_classes import snapshot
from tests.integration.ledger.test_posting_service_database import ledger_setup as ledger_setup

pytestmark = pytest.mark.integration


def fixture(s, direction="payout"):
    payment, _paid, allocation, request = setup_return(s, direction=direction)
    returned = submit_return(s, request)
    return payment, allocation, returned


def write(
    s,
    source,
    *,
    asset="USD",
    amount="20",
    principal="20",
    mutation=None,
    receipt_mutation=None,
    omit_reference=False,
    fees=(),
    missing_clearing=False,
    day="2026-10-02",
):
    payment, _allocation, returned = source
    payload = BusinessRefundCreate(
        id=uuid4(),
        payment_id=payment.id,
        expected_version=1,
        account_id=payment.account_id,
        asset_id=asset,
        amount=amount,
        payment_amount=principal,
        transaction_date=day,
        reason="Fictional direct refund validation 中文",
        allocation_return_ids=[] if omit_reference else [returned.id],
        fees=list(fees),
    )
    # Deliberately bypass the app capacity planner: these tests exercise database enforcement.
    cash, original = Amount.parse(amount, ASSETS[asset]), Amount.parse(principal, ASSETS["USD"])
    sign = -1 if payment.direction == "receipt" else 1
    key = "advance.received" if sign == -1 else "advance.paid"
    plan = RefundPlan(
        key,
        cash if sign == 1 else -cash,
        -original if sign == 1 else original,
        original,
        Amount(ASSETS["USD"], 0),
        (),
    )
    service = BusinessSettlementService(s["engine"])
    with Session(s["engine"]) as session, session.begin():
        session.execute(text("SET TRANSACTION ISOLATION LEVEL READ COMMITTED"))
        acquire_write_lock(session)
        service._locked_ledger(session, s["owner"], s["ledger"])
        controls = ControlAccounts(s["engine"]).ensure(
            session,
            s["owner"],
            s["ledger"],
            {key: {"USD"}},
            lock_assets={"USD", asset} | {f.asset_id for f in payload.fees},
        )
        group = refund_rows(
            service, session, s["ledger"], payment.party_id, payload, plan, controls, ASSETS
        )
        if missing_clearing:
            group = SimpleNamespace(**(vars(group) | {"parameters": group.parameters[:2]}))
        now = session.scalar(select(func.clock_timestamp()))
        idempotency_key = str(uuid4())
        record = dict(
            id=payload.id,
            ledger_id=s["ledger"],
            payment_id=payment.id,
            source_version=1,
            account_id=payload.account_id,
            asset_id=asset,
            amount=cash.to_decimal(),
            payment_amount=original.to_decimal(),
            transaction_date=payload.transaction_date,
            reason=payload.reason,
            fees=[f.model_dump(mode="json") for f in payload.fees],
            created_by=s["owner"],
            idempotency_key=idempotency_key,
            operation_id=group.operation_id,
            journal_id=group.journal_id,
            created_at=now,
        )
        record.update(mutation or {})
        session.execute(insert(BusinessRefund).values(**record))
        if not omit_reference:
            session.execute(
                insert(BusinessRefundReturn).values(
                    refund_id=payload.id,
                    payment_id=payment.id,
                    ledger_id=s["ledger"],
                    return_id=returned.id,
                    line_no=1,
                    created_at=now,
                )
            )
        persist_posting(
            session, s["owner"], s["ledger"], "refund", payload.transaction_date, group, now
        )
        response = session.scalar(
            text("SELECT coinpup_business_refund_response(:id)"), {"id": payload.id}
        )
        session.execute(
            insert(CommandReceipt).values(
                ledger_id=s["ledger"],
                key=idempotency_key,
                hash_version=2,
                request_hash="f" * 64,
                operation_id=group.operation_id,
                response_status=201,
                response=response | (receipt_mutation or {}),
                created_at=now,
            )
        )
    return BusinessRefundResponse.model_validate(response)


@pytest.mark.parametrize("direction", ["receipt", "payout"])
@pytest.mark.parametrize("asset,amount", [("USD", "20"), ("EUR", "19.37"), ("BTC", "0.00000001")])
def test_exact_same_and_cross_asset_refund_commits_and_is_delivered(
    drafts, direction, asset, amount
):
    s = drafts
    source = fixture(s, direction)
    result = write(s, source, asset=asset, amount=amount)
    assert result.payment_id == source[0].id and result.payment_amount == "20.000000000000000000"
    assert (
        Amount.from_decimal(Decimal(result.amount), ASSETS[asset]).minor_units
        == Amount.parse(amount, ASSETS[asset]).minor_units
    )
    changes, cursor = [], "0"
    while True:
        page = ChangeService(s["engine"]).list_changes(s["owner"], after=cursor, limit=200)
        if not page.changes:
            break
        changes.extend(page.changes)
        cursor = page.next_cursor
    assert {c.entity_type for c in changes} >= {"business_refunds", "business_refund_returns"}


@pytest.mark.parametrize(
    "changes",
    [
        {"mutation": {"source_version": 2}},
        {"day": "2026-09-14"},
        {"day": "2026-09-30"},
        {"mutation": {"reason": "Fictional wrong header"}},
    ],
)
def test_refund_rejects_changed_source_dates_or_header_atomically(drafts, changes):
    s = drafts
    source = fixture(s)
    before = snapshot(s)
    with pytest.raises(IntegrityError) as error:
        write(s, source, **changes)
    assert error.value.orig.diag.constraint_name == "ck_business_refund_shape"
    assert snapshot(s) == before


@pytest.mark.parametrize(
    "changes",
    [
        # Keep prepared legs balanced, then corrupt only the stored same-asset quantity.
        {"mutation": {"amount": Decimal("19")}},
        {"principal": "21", "amount": "21"},
        {"receipt_mutation": {"payment_amount": "19"}},
        {"asset": "EUR", "amount": "19.37", "missing_clearing": True},
    ],
)
def test_refund_rejects_inconsistent_amount_capacity_receipt_or_balance(drafts, changes):
    s = drafts
    source = fixture(s)
    before = snapshot(s)
    with pytest.raises(IntegrityError):
        write(s, source, **changes)
    assert snapshot(s) == before


def test_refunded_principal_cannot_be_refunded_or_allocated_again(drafts):
    s = drafts
    source = fixture(s)
    write(s, source, principal="10", amount="10", omit_reference=True)
    write(s, source, principal="10", amount="10")
    before = snapshot(s)
    with pytest.raises(IntegrityError):
        write(s, source, principal="0.01", amount="0.01")
    original = source[1]
    request = BusinessAllocationCreate.model_validate(
        original.model_dump()
        | {
            "id": uuid4(),
            "allocation_date": "2026-10-02",
            "lines": [
                original.lines[0].model_dump() | {"payment_amount": "1", "document_amount": "1"}
            ],
        }
    )
    with pytest.raises(LedgerError) as error:
        BusinessSettlementService(s["engine"]).allocate(
            s["owner"], s["ledger"], request, str(uuid4())
        )
    assert error.value.code == "constraint_conflict"
    assert snapshot(s) == before


def test_concurrent_refunds_cannot_overconsume_released_principal(drafts):
    s = drafts
    source = fixture(s)

    def attempt(_):
        try:
            return write(s, source, principal="10.01", amount="10.01")
        except IntegrityError as error:
            assert error.orig.diag.constraint_name == "ck_business_refund_shape"
            return None

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(attempt, range(2)))
    assert sum(result is not None for result in results) == 1
    with s["engine"].connect() as c:
        assert c.scalar(select(func.count()).select_from(BusinessRefund)) == 1


def test_valid_fees_are_independent_and_refund_history_prevents_downgrade(drafts):
    s = drafts
    source = fixture(s)
    fees = [
        dict(
            account_id=s["account"].id,
            asset_id="USD",
            amount="0.01",
            category_id=s["expenses"][0].id,
        )
    ]
    result = write(s, source, asset="EUR", amount="19.37", fees=fees)
    assert result.fees[0].amount == "0.01"
    before = snapshot(s)
    with pytest.raises(IntegrityError) as error, s["engine"].begin() as c:
        with Operations.context(MigrationContext.configure(c)):
            refund_validation_migration()["downgrade"]()
    assert error.value.orig.diag.constraint_name == "ck_business_refund_downgrade"
    assert snapshot(s) == before


@pytest.mark.parametrize(
    "change",
    [{"amount": "0.00"}, {"amount": "0.010"}, {"amount": "0.02"}, {"extra": "fictional"}],
)
def test_fee_storage_cannot_bypass_positive_exact_shape_or_match_different_rows(drafts, change):
    s = drafts
    source = fixture(s)
    fee = dict(
        account_id=str(s["account"].id),
        asset_id="USD",
        amount="0.01",
        category_id=str(s["expenses"][0].id),
    )
    before = snapshot(s)
    with pytest.raises(IntegrityError) as error:
        write(s, source, fees=[fee], mutation={"fees": [fee | change]})
    assert error.value.orig.diag.constraint_name == "ck_business_refund_shape"
    assert snapshot(s) == before
