"""Sum only explicitly attributed principal, in the actual payment asset and transaction period."""

from sqlalchemy import and_, case, exists, func, select, union
from sqlalchemy.orm import aliased

from coinpup_api.business.allocation_return_models import BusinessAllocationReturnLine
from coinpup_api.business.allocation_return_revision_member_models import (
    BusinessReturnRevisionMember,
)
from coinpup_api.business.settlement_models import BusinessAllocationLine, BusinessPayment
from coinpup_api.ledger.models import Account, Journal, JournalLine


def project_payment_buckets(ledger, project, start, end):
    # A return correction/reversal belongs to the same immutable member operation.
    # Later-created members have a durable identity too; never join every historical
    # member version to each journal, which would multiply fees and principal.
    owners = union(
        *(
            select(source.operation_id, source.payment_id).where(source.ledger_id == ledger)
            for source in (
                BusinessAllocationLine,
                BusinessAllocationReturnLine,
                BusinessReturnRevisionMember,
            )
        )
    ).subquery()
    pay, journal, principal = BusinessPayment, Journal, JournalLine
    document = aliased(JournalLine)
    project_side = exists(
        select(1).where(
            document.ledger_id == ledger,
            document.journal_id == journal.id,
            document.role == "account",
            document.project_id == project,
            document.document_id.is_not(None),
            document.document_line_id.is_not(None),
        )
    ).correlate(journal)
    return (
        select(
            pay.asset_id.label("asset_id"),
            func.sum(case((pay.direction == "receipt", principal.amount), else_=0)).label(
                "received"
            ),
            func.sum(case((pay.direction == "payout", -principal.amount), else_=0)).label("paid"),
        )
        .select_from(pay)
        .join(owners, owners.c.payment_id == pay.id)
        .join(
            journal,
            and_(journal.ledger_id == ledger, journal.operation_id == owners.c.operation_id),
        )
        .join(
            principal,
            and_(
                principal.ledger_id == ledger,
                principal.journal_id == journal.id,
                principal.asset_id == pay.asset_id,
                principal.role == "account",
            ),
        )
        .join(
            Account,
            and_(
                Account.ledger_id == ledger,
                Account.id == principal.account_id,
                Account.system_key
                == case((pay.direction == "receipt", "advance.received"), else_="advance.paid"),
            ),
        )
        .where(
            pay.ledger_id == ledger,
            pay.transaction_date.between(start, end),
            journal.sealed.is_(True),
            journal.recognition_date <= end,
            project_side,
        )
        .group_by(pay.asset_id)
    )
