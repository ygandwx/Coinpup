"""Prepared immutable posting groups for payment and explicit source-line allocations."""

from dataclasses import dataclass
from uuid import UUID, uuid4

from sqlalchemy import insert

from coinpup_api.business.settlement_models import BusinessAllocation, BusinessAllocationLine
from coinpup_api.business.settlement_schemas import AllocationPosting, BusinessAllocationResponse
from coinpup_api.ledger.models import FinancialOperation, Journal, JournalLine
from coinpup_api.ledger.posting_storage import PostingLine, prepare_journal_lines


@dataclass(frozen=True)
class PostingGroup:
    operation_id: UUID
    journal_id: UUID
    description: str
    lines: tuple[PostingLine, ...]
    parameters: tuple[dict, ...]


def payment_rows(service, session, ledger_id, payload, plan, controls, catalog):
    operation, journal = uuid4(), uuid4()
    common = dict(
        journal_id=journal,
        ledger_id=ledger_id,
        asset_id=payload.asset_id,
        role="account",
        party_id=payload.party_id,
    )
    rows = [
        PostingLine(line_no=1, account_id=payload.account_id, amount=plan.bank_amount, **common),
        PostingLine(
            line_no=2,
            account_id=controls[plan.control_key].id,
            amount=plan.advance_amount,
            **common,
        ),
    ]
    service._append_fees(session, ledger_id, rows, payload.fees, catalog)
    return PostingGroup(
        operation,
        journal,
        payload.description,
        tuple(rows),
        tuple(prepare_journal_lines(rows, catalog)),
    )


def allocation_rows(ledger_id, party_id, plan, controls, sources, catalog, *, description=None):
    groups = []
    for item in plan.lines:
        operation, journal = uuid4(), uuid4()
        principal = dict(party_id=party_id)
        dimensions = dict(
            party_id=party_id,
            document_id=item.source.document_id,
            document_line_id=item.source.document_line_id,
            project_id=item.source.project_id,
        )
        rows = [
            PostingLine(
                journal_id=journal,
                ledger_id=ledger_id,
                line_no=1,
                role="account",
                asset_id=item.payment_amount.asset.asset_id,
                amount=item.advance_amount,
                account_id=controls[plan.advance_key].id,
                **principal,
            ),
            PostingLine(
                journal_id=journal,
                ledger_id=ledger_id,
                line_no=2,
                role="account",
                asset_id=item.document_amount.asset.asset_id,
                amount=item.document_control_amount,
                account_id=controls[plan.document_control_key].id,
                **dimensions,
            ),
        ]
        if item.cross_asset:
            rows.extend(
                [
                    PostingLine(
                        journal_id=journal,
                        ledger_id=ledger_id,
                        line_no=3,
                        role="exchange",
                        asset_id=item.payment_amount.asset.asset_id,
                        amount=-item.advance_amount,
                        **principal,
                    ),
                    PostingLine(
                        journal_id=journal,
                        ledger_id=ledger_id,
                        line_no=4,
                        role="exchange",
                        asset_id=item.document_amount.asset.asset_id,
                        amount=-item.document_control_amount,
                        **dimensions,
                    ),
                ]
            )
        groups.append(
            PostingGroup(
                operation,
                journal,
                sources[item.source.document_line_id][1].description
                if description is None
                else description,
                tuple(rows),
                tuple(prepare_journal_lines(rows, catalog)),
            )
        )
    return tuple(groups)


def persist_posting(session, owner_id, ledger_id, kind, day, group, now):
    session.execute(
        insert(FinancialOperation).values(
            id=group.operation_id,
            ledger_id=ledger_id,
            kind=kind,
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
            transaction_date=day,
            recognition_date=day,
            description=group.description,
            created_at=now,
        )
    )
    session.execute(insert(JournalLine), list(group.parameters))


def persist_allocation(session, owner_id, ledger_id, payload, payment_id, key, plan, groups, now):
    session.execute(
        insert(BusinessAllocation).values(
            id=payload.id,
            ledger_id=ledger_id,
            payment_id=payment_id,
            allocation_date=plan.allocation_date,
            created_by=owner_id,
            idempotency_key=key,
            created_at=now,
        )
    )
    members = []
    for item, group in zip(plan.lines, groups, strict=True):
        persist_posting(
            session, owner_id, ledger_id, "allocation", plan.allocation_date, group, now
        )
        session.execute(
            insert(BusinessAllocationLine).values(
                ledger_id=ledger_id,
                allocation_id=payload.id,
                payment_id=payment_id,
                document_id=item.source.document_id,
                document_line_id=item.source.document_line_id,
                line_no=item.line_no,
                payment_amount=item.payment_amount.to_decimal(),
                document_amount=item.document_amount.to_decimal(),
                operation_id=group.operation_id,
                journal_id=group.journal_id,
                created_at=now,
            )
        )
        members.append(
            AllocationPosting(
                line_no=item.line_no,
                document_id=item.source.document_id,
                document_line_id=item.source.document_line_id,
                operation_id=group.operation_id,
                journal_id=group.journal_id,
            )
        )
    return BusinessAllocationResponse(
        kind="business_allocation",
        id=payload.id,
        ledger_id=ledger_id,
        payment_id=payment_id,
        postings=tuple(members),
    )
