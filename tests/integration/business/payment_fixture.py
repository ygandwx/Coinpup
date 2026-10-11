"""Fictional committed payment facts and deliberate raw-SQL constraint probes."""

from uuid import UUID, uuid4

from coinpup_api.business.models import BusinessParty
from coinpup_api.business.settlement_hash import payment_hash_v2
from coinpup_api.business.settlement_models import BusinessPayment
from coinpup_api.business.settlement_schemas import BusinessPaymentCreate, BusinessPaymentResponse
from coinpup_api.ledger.assets import ASSETS
from coinpup_api.ledger.control_accounts import ControlAccounts
from coinpup_api.ledger.models import CommandReceipt, FinancialOperation, Journal, JournalLine
from coinpup_api.ledger.money import Amount
from coinpup_api.ledger.posting_storage import PostingLine, prepare_journal_lines
from sqlalchemy import insert

from tests.integration.ledger.test_posting_service_database import opening


def source(s, direction="payout", *, asset="USD", amount="110.00", fee_asset="USD", fee_count=1):
    for code in {asset, fee_asset}:
        s["posting"].post_opening(
            s["owner"], s["ledger"], opening(s, "1000", code), "fictional-seed-" + code
        )
    party = uuid4()
    with s["engine"].begin() as c:
        c.execute(
            insert(BusinessParty).values(
                id=party, ledger_id=s["ledger"], name="Fictional payment partner", role="both"
            )
        )
    control_key = "advance.received" if direction == "receipt" else "advance.paid"
    service = ControlAccounts(s["engine"])
    with service._transaction(s["owner"], write=True) as session:
        control = service.ensure(
            session, s["owner"], s["ledger"], {control_key: {asset}}, lock_assets={asset, fee_asset}
        )[control_key].id
    payload = BusinessPaymentCreate(
        id=uuid4(),
        direction=direction,
        party_id=party,
        account_id=s["account"].id,
        asset_id=asset,
        amount=amount,
        transaction_date="2026-09-15",
        description="Fictional payment",
        fees=[
            {
                "account_id": s["account"].id,
                "asset_id": fee_asset,
                "amount": "1",
                "category_id": s["expenses"][0].id,
            }
            for _ in range(fee_count)
        ],
    )
    return payload, control


def write(
    c,
    s,
    payload,
    control,
    *,
    payment_changes=None,
    journal_changes=None,
    receipt_changes=None,
    change_rows=None,
    omit_payment=False,
    initial_allocation=None,
):
    c.exec_driver_sql("SELECT pg_advisory_xact_lock(18945999704708432)")
    operation, journal = uuid4(), uuid4()
    key = "fictional-payment-" + str(payload.id)
    quantity = Amount.parse(payload.amount, ASSETS[payload.asset_id])
    bank = quantity if payload.direction == "receipt" else -quantity
    lines = [
        PostingLine(
            journal_id=journal,
            ledger_id=s["ledger"],
            line_no=1,
            role="account",
            asset_id=payload.asset_id,
            amount=bank,
            account_id=payload.account_id,
            party_id=payload.party_id,
        ),
        PostingLine(
            journal_id=journal,
            ledger_id=s["ledger"],
            line_no=2,
            role="account",
            asset_id=payload.asset_id,
            amount=-bank,
            account_id=control,
            party_id=payload.party_id,
        ),
    ]
    for component, fee in enumerate(payload.fees, 1):
        amount = Amount.parse(fee.amount, ASSETS[fee.asset_id])
        lines.extend(
            [
                PostingLine(
                    journal_id=journal,
                    ledger_id=s["ledger"],
                    line_no=len(lines) + 1,
                    role="account",
                    asset_id=fee.asset_id,
                    amount=-amount,
                    account_id=fee.account_id,
                    component_no=component,
                ),
                PostingLine(
                    journal_id=journal,
                    ledger_id=s["ledger"],
                    line_no=len(lines) + 2,
                    role="expense",
                    asset_id=fee.asset_id,
                    amount=amount,
                    category_id=fee.category_id,
                    component_no=component,
                ),
            ]
        )
    parameters = prepare_journal_lines(lines, ASSETS)
    if change_rows:
        change_rows(parameters)
    if not omit_payment:
        values = dict(
            id=payload.id,
            ledger_id=s["ledger"],
            party_id=payload.party_id,
            account_id=payload.account_id,
            asset_id=payload.asset_id,
            direction=payload.direction,
            amount=quantity.to_decimal(),
            transaction_date=payload.transaction_date,
            description=payload.description,
            fees=[f.model_dump(mode="json") for f in payload.fees],
            operation_id=operation,
            journal_id=journal,
            initial_allocation_id=UUID(initial_allocation["id"]) if initial_allocation else None,
            created_by=s["owner"],
            idempotency_key=key,
        )
        c.execute(insert(BusinessPayment).values(values | (payment_changes or {})))
    c.execute(
        insert(FinancialOperation).values(
            id=operation,
            ledger_id=s["ledger"],
            kind="payment",
            current_journal_id=journal,
            created_by=s["owner"],
        )
    )
    header = dict(
        id=journal,
        operation_id=operation,
        ledger_id=s["ledger"],
        transaction_date=payload.transaction_date,
        recognition_date=payload.transaction_date,
        description=payload.description,
    )
    c.execute(insert(Journal).values(header | (journal_changes or {})))
    c.execute(insert(JournalLine), parameters)
    result = BusinessPaymentResponse(
        kind="business_payment",
        id=payload.id,
        ledger_id=s["ledger"],
        operation_id=operation,
        journal_id=journal,
        initial_allocation=initial_allocation,
    ).model_dump(mode="json")
    receipt = dict(
        ledger_id=s["ledger"],
        key=key,
        hash_version=2,
        request_hash=payment_hash_v2(s["ledger"], payload),
        response=result,
        response_status=201,
        operation_id=operation,
    )
    c.execute(insert(CommandReceipt).values(receipt | (receipt_changes or {})))
    return result
