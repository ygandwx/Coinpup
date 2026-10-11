"""Explicit fictional SQL fixture for the later accrual activation constraints."""

from uuid import UUID, uuid4

from coinpup_api.business.confirmation_hash import confirmation_hash_v2
from coinpup_api.business.confirmation_models import BusinessConfirmation, BusinessPosting
from coinpup_api.business.confirmation_schemas import BusinessConfirmationCreate
from coinpup_api.business.models import BusinessDocument
from coinpup_api.business.pricing import PriceInput
from coinpup_api.ledger.assets import ASSETS
from coinpup_api.ledger.models import CommandReceipt, FinancialOperation, Journal, JournalLine
from coinpup_api.ledger.money import Amount
from coinpup_api.ledger.posting_storage import PostingLine, prepare_journal_lines
from sqlalchemy import insert, update

from tests.integration.business.test_draft_headers import fields, write
from tests.integration.business.test_draft_lines import line, save
from tests.integration.ledger.test_control_accounts import acquire


def source(s, kind="invoice"):
    header = fields(s) | {"document_kind": kind}
    parent = write(s, header)
    row = line(
        s,
        parent,
        PriceInput("1", "100.00", tax_rate_percent="10"),
        category_kind="income" if kind == "invoice" else "expense",
        category_id=s["income"].id if kind == "invoice" else s["expenses"][0].id,
    )
    save(s, row)
    control = "receivable.customer" if kind == "invoice" else "payable.supplier"
    account = acquire(s, {control: {"USD"}})[control]
    return parent, header, [row], account


def confirm_rows(c, s, parent, header, rows, account, *, change=None, receipt_change=None):
    identifier = uuid4()
    key = "fictional-confirm-" + str(identifier)
    c.exec_driver_sql("SELECT pg_advisory_xact_lock(18945999704708432)")
    c.execute(
        insert(BusinessConfirmation).values(
            id=identifier,
            ledger_id=s["ledger"],
            document_id=parent,
            created_by=s["owner"],
            source_version=1,
            idempotency_key=key,
        )
    )
    c.execute(
        update(BusinessDocument)
        .where(BusinessDocument.id == parent)
        .values(state="confirmed", version=2)
    )
    result = {
        "kind": "business_confirmation",
        "id": str(identifier),
        "ledger_id": str(s["ledger"]),
        "document_id": str(parent),
        "source_version": 1,
        "document_version": 2,
        "postings": [],
    }
    for row in sorted(rows, key=lambda row: row["line_no"]):
        amount = Amount.from_decimal(row["total_amount"], ASSETS["USD"])
        if amount.minor_units == 0:
            continue
        operation, journal = uuid4(), uuid4()
        sign = 1 if header["document_kind"] == "invoice" else -1
        account_amount = amount if sign == 1 else -amount
        c.execute(
            insert(FinancialOperation).values(
                id=operation,
                ledger_id=s["ledger"],
                kind=header["document_kind"] + "_accrual",
                current_journal_id=journal,
                created_by=s["owner"],
            )
        )
        c.execute(
            insert(Journal).values(
                id=journal,
                operation_id=operation,
                ledger_id=s["ledger"],
                transaction_date=header["issue_date"],
                recognition_date=row["recognition_date"],
                description=row["description"],
            )
        )
        dimensions = dict(
            party_id=header["party_id"],
            document_id=parent,
            document_line_id=row["id"],
            project_id=row["project_id"],
        )
        journal_rows = prepare_journal_lines(
            [
                PostingLine(
                    journal_id=journal,
                    ledger_id=s["ledger"],
                    line_no=1,
                    role="account",
                    asset_id="USD",
                    amount=account_amount,
                    account_id=account,
                    **dimensions,
                ),
                PostingLine(
                    journal_id=journal,
                    ledger_id=s["ledger"],
                    line_no=2,
                    role=row["category_kind"],
                    asset_id="USD",
                    amount=-account_amount,
                    category_id=row["category_id"],
                    **dimensions,
                ),
            ],
            ASSETS,
        )
        if change:
            change(journal_rows, row)
        c.execute(insert(JournalLine), journal_rows)
        c.execute(
            insert(BusinessPosting).values(
                ledger_id=s["ledger"],
                confirmation_id=identifier,
                document_id=parent,
                line_id=row["id"],
                operation_id=operation,
                journal_id=journal,
            )
        )
        result["postings"].append(
            {
                "line_id": str(row["id"]),
                "line_no": row["line_no"],
                "operation_id": str(operation),
                "journal_id": str(journal),
            }
        )
    if receipt_change:
        receipt_change(result)
    c.execute(
        insert(CommandReceipt).values(
            ledger_id=s["ledger"],
            key=key,
            hash_version=2,
            request_hash=confirmation_hash_v2(
                s["ledger"], parent, BusinessConfirmationCreate(id=identifier, expected_version=1)
            ),
            response=result,
            response_status=201,
            operation_id=UUID(result["postings"][0]["operation_id"]),
        )
    )
    return result
