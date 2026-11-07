"""Credit postings retain original dimensions and explicit new recognition dates."""

from uuid import uuid4

from sqlalchemy import insert

from coinpup_api.business.credit_models import BusinessCredit, BusinessCreditLine
from coinpup_api.business.settlement_rows import PostingGroup
from coinpup_api.ledger.models import FinancialOperation, Journal, JournalLine
from coinpup_api.ledger.posting_storage import PostingLine, prepare_journal_lines


def credit_rows(ledger_id, document, request, plan, account_id, catalog):
    operation_id, journal_id = uuid4(), uuid4()
    return credit_rows_for_identity(
        ledger_id,
        document,
        request,
        plan,
        account_id,
        catalog,
        operation_id=operation_id,
        journal_id=journal_id,
    )


def credit_rows_for_identity(
    ledger_id, document, request, plan, account_id, catalog, *, operation_id, journal_id
):
    """Reuse the existing two rows when a correction retains its operation identity."""
    scope = dict(
        journal_id=journal_id,
        ledger_id=ledger_id,
        asset_id=document.asset_id,
        party_id=document.party_id,
        document_id=document.id,
        document_line_id=plan.line_id,
        project_id=plan.project_id,
    )
    rows = [
        PostingLine(
            **scope, line_no=1, role="account", account_id=account_id, amount=plan.control_amount
        ),
        PostingLine(
            **scope,
            line_no=2,
            role=plan.category_role,
            category_id=plan.category_id,
            amount=plan.category_amount,
        ),
    ]
    return PostingGroup(
        operation_id,
        journal_id,
        request.reason,
        tuple(rows),
        tuple(prepare_journal_lines(rows, catalog)),
    )


def persist_credit(session, owner_id, ledger_id, payload, key, groups, now):
    session.execute(
        insert(BusinessCredit).values(
            id=payload.id,
            ledger_id=ledger_id,
            document_id=payload.document_id,
            source_version=payload.expected_version,
            credit_date=payload.credit_date,
            reason=payload.reason,
            created_by=owner_id,
            idempotency_key=key,
            created_at=now,
        )
    )
    for position, (requested, original, plan, group) in enumerate(groups, 1):
        session.execute(
            insert(FinancialOperation).values(
                id=group.operation_id,
                ledger_id=ledger_id,
                kind="invoice_credit" if plan.category_role == "income" else "bill_credit",
                current_journal_id=group.journal_id,
                created_by=owner_id,
                created_at=now,
                updated_at=now,
            )
        )
        session.execute(
            insert(Journal).values(
                id=group.journal_id,
                operation_id=group.operation_id,
                ledger_id=ledger_id,
                transaction_date=payload.credit_date,
                recognition_date=requested.recognition_date,
                description=payload.reason,
                created_at=now,
            )
        )
        session.execute(insert(JournalLine), list(group.parameters))
        session.execute(
            insert(BusinessCreditLine).values(
                credit_id=payload.id,
                document_id=payload.document_id,
                ledger_id=ledger_id,
                source_line_id=requested.source_line_id,
                source_operation_id=original.operation_id,
                line_no=position,
                net_amount=plan.net.to_decimal(),
                tax_amount=plan.tax.to_decimal(),
                total_amount=plan.total.to_decimal(),
                recognition_date=requested.recognition_date,
                operation_id=group.operation_id,
                journal_id=group.journal_id,
                created_at=now,
            )
        )
