"""Fictional confirmed sources and exact allocation SQL for database constraint probes."""

from uuid import UUID, uuid4

from coinpup_api.business.settlement_hash import allocation_hash_v2
from coinpup_api.business.settlement_models import BusinessAllocation, BusinessAllocationLine
from coinpup_api.business.settlement_schemas import (
    BusinessAllocationCreate,
    InitialAllocationCreate,
)
from coinpup_api.ledger.assets import ASSETS
from coinpup_api.ledger.control_accounts import ControlAccounts
from coinpup_api.ledger.models import CommandReceipt, FinancialOperation, Journal, JournalLine
from coinpup_api.ledger.money import Amount
from coinpup_api.ledger.posting_storage import PostingLine, prepare_journal_lines
from sqlalchemy import insert

from tests.integration.business.payment_fixture import source as payment_source
from tests.integration.business.payment_fixture import write as payment_write
from tests.integration.business.test_confirmation_service import prepare, submit


def source(s, direction="payout", *, payment_asset="USD", document_asset="USD", committed=True):
    payment, advance = payment_source(s, direction, asset=payment_asset, fee_asset=payment_asset)
    document, request = prepare(
        s,
        kind="invoice" if direction == "receipt" else "bill",
        changes={
            "party_id": payment.party_id,
            "asset_id": document_asset,
            "issue_date": "2026-09-30",
        },
    )
    submit(s, document, request, "fictional-confirm-" + str(document.id))
    key = "receivable.customer" if direction == "receipt" else "payable.supplier"
    controls = ControlAccounts(s["engine"])
    with controls._transaction(s["owner"], write=True) as session:
        control = controls.ensure(
            session,
            s["owner"],
            s["ledger"],
            {key: {document_asset}},
            lock_assets={payment_asset, document_asset},
        )[key].id
    if committed:
        with s["engine"].begin() as c:
            payment_write(c, s, payment, advance)
    return payment, advance, document, control


def write(
    c,
    s,
    payment,
    advance,
    document,
    control,
    *,
    payment_amount="110",
    document_amount="110",
    allocation_date="2026-09-30",
    group_changes=None,
    line_changes=None,
    header_changes=None,
    receipt_changes=None,
    change_rows=None,
    initial=False,
    omit_line=False,
):
    c.exec_driver_sql("SELECT pg_advisory_xact_lock(18945999704708432)")
    identifier, operation, journal = uuid4(), uuid4(), uuid4()
    row = document.lines[0]
    body = BusinessAllocationCreate(
        id=identifier,
        payment_id=payment.id,
        allocation_date=allocation_date,
        lines=[
            dict(
                document_id=document.id,
                document_line_id=row.id,
                payment_amount=payment_amount,
                document_amount=document_amount,
            )
        ],
    )
    result = dict(
        kind="business_allocation",
        id=str(identifier),
        ledger_id=str(s["ledger"]),
        payment_id=str(payment.id),
        postings=[
            dict(
                line_no=1,
                document_id=str(document.id),
                document_line_id=str(row.id),
                operation_id=str(operation),
                journal_id=str(journal),
            )
        ],
    )
    key = (
        "fictional-payment-" + str(payment.id)
        if initial
        else "fictional-allocation-" + str(identifier)
    )
    if initial:
        payment = payment.model_copy(
            update={
                "initial_allocation": InitialAllocationCreate(
                    **body.model_dump(exclude={"payment_id"})
                )
            }
        )
        payment_write(c, s, payment, advance, initial_allocation=result)
    c.execute(
        insert(BusinessAllocation).values(
            dict(
                id=identifier,
                ledger_id=s["ledger"],
                payment_id=payment.id,
                allocation_date=body.allocation_date,
                created_by=s["owner"],
                idempotency_key=key,
            )
            | (group_changes or {})
        )
    )
    c.execute(
        insert(FinancialOperation).values(
            id=operation,
            ledger_id=s["ledger"],
            kind="allocation",
            current_journal_id=journal,
            created_by=s["owner"],
        )
    )
    c.execute(
        insert(Journal).values(
            dict(
                id=journal,
                operation_id=operation,
                ledger_id=s["ledger"],
                transaction_date=body.allocation_date,
                recognition_date=body.allocation_date,
                description=row.description,
            )
            | (header_changes or {})
        )
    )
    paid = Amount.parse(payment_amount, ASSETS[payment.asset_id])
    applied = Amount.parse(document_amount, ASSETS[document.asset_id])
    if payment.direction != "receipt":
        paid = -paid
    else:
        applied = -applied
    dimensions = dict(
        party_id=payment.party_id,
        document_id=document.id,
        document_line_id=row.id,
        project_id=row.project_id,
    )
    principal = dict(party_id=payment.party_id)
    rows = [
        PostingLine(
            journal_id=journal,
            ledger_id=s["ledger"],
            line_no=1,
            role="account",
            asset_id=payment.asset_id,
            amount=paid,
            account_id=advance,
            **principal,
        ),
        PostingLine(
            journal_id=journal,
            ledger_id=s["ledger"],
            line_no=2,
            role="account",
            asset_id=document.asset_id,
            amount=applied,
            account_id=control,
            **dimensions,
        ),
    ]
    if payment.asset_id != document.asset_id:
        rows.extend(
            [
                PostingLine(
                    journal_id=journal,
                    ledger_id=s["ledger"],
                    line_no=3,
                    role="exchange",
                    asset_id=payment.asset_id,
                    amount=-paid,
                    **principal,
                ),
                PostingLine(
                    journal_id=journal,
                    ledger_id=s["ledger"],
                    line_no=4,
                    role="exchange",
                    asset_id=document.asset_id,
                    amount=-applied,
                    **dimensions,
                ),
            ]
        )
    parameters = prepare_journal_lines(rows, ASSETS)
    if change_rows:
        change_rows(parameters)
    c.execute(insert(JournalLine), parameters)
    if not omit_line:
        c.execute(
            insert(BusinessAllocationLine).values(
                dict(
                    ledger_id=s["ledger"],
                    allocation_id=identifier,
                    payment_id=payment.id,
                    document_id=document.id,
                    document_line_id=row.id,
                    line_no=1,
                    payment_amount=Amount.parse(
                        payment_amount, ASSETS[payment.asset_id]
                    ).to_decimal(),
                    document_amount=Amount.parse(
                        document_amount, ASSETS[document.asset_id]
                    ).to_decimal(),
                    operation_id=operation,
                    journal_id=journal,
                )
                | (line_changes or {})
            )
        )
    if not initial:
        c.execute(
            insert(CommandReceipt).values(
                dict(
                    ledger_id=s["ledger"],
                    key=key,
                    hash_version=2,
                    response_status=201,
                    request_hash=allocation_hash_v2(s["ledger"], body),
                    response=result,
                    operation_id=UUID(result["postings"][0]["operation_id"]),
                )
                | (receipt_changes or {})
            )
        )
    return result
