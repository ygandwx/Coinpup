"""Exact net applications in one query, without multiplying joined source rows."""

from sqlalchemy import func, literal, select, union_all

from coinpup_api.business.allocation_return_models import BusinessAllocationReturnLine
from coinpup_api.business.credit_models import BusinessCreditLine
from coinpup_api.business.refund_models import BusinessRefund
from coinpup_api.business.settlement_models import BusinessAllocationLine


def document_totals(session, ledger_id, identifiers):
    """Keep net settlements and manual credit components separate in one bounded query."""
    if not identifiers:
        return {}
    source, returned, credit = (
        BusinessAllocationLine,
        BusinessAllocationReturnLine,
        BusinessCreditLine,
    )
    positive = select(
        source.document_line_id.label("identity"),
        source.document_amount.label("applied"),
        literal(0).label("credited_net"),
        literal(0).label("credited_tax"),
    ).where(source.ledger_id == ledger_id, source.document_line_id.in_(identifiers))
    negative = (
        select(source.document_line_id, -returned.document_amount, literal(0), literal(0))
        .join(
            returned,
            (returned.source_allocation_line_id == source.id)
            & (returned.ledger_id == source.ledger_id),
        )
        .where(
            source.ledger_id == ledger_id,
            returned.ledger_id == ledger_id,
            source.document_line_id.in_(identifiers),
        )
    )
    credits = select(credit.source_line_id, literal(0), credit.net_amount, credit.tax_amount).where(
        credit.ledger_id == ledger_id, credit.source_line_id.in_(identifiers)
    )
    entries = union_all(positive, negative, credits).subquery()
    return {
        identifier: (applied, net, tax)
        for identifier, applied, net, tax in session.execute(
            select(
                entries.c.identity,
                func.sum(entries.c.applied),
                func.sum(entries.c.credited_net),
                func.sum(entries.c.credited_tax),
            ).group_by(entries.c.identity)
        )
    }


def payment_totals(session, ledger_id, identifiers):
    """Keep net applications and refunded principal distinct in one bounded page query."""
    if not identifiers:
        return {}
    source, returned, refund = BusinessAllocationLine, BusinessAllocationReturnLine, BusinessRefund
    statements = [
        select(
            table.payment_id.label("identity"),
            applied.label("applied"),
            refunded.label("refunded"),
        ).where(table.ledger_id == ledger_id, table.payment_id.in_(identifiers))
        for table, applied, refunded in (
            (source, source.payment_amount, literal(0)),
            (returned, -returned.payment_amount, literal(0)),
            (refund, literal(0), refund.payment_amount),
        )
    ]
    entries = union_all(*statements).subquery()
    return {
        identifier: (applied, refunded)
        for identifier, applied, refunded in session.execute(
            select(
                entries.c.identity, func.sum(entries.c.applied), func.sum(entries.c.refunded)
            ).group_by(entries.c.identity)
        )
    }


def applied_totals(session, ledger_id, identifiers, *, by_document_line=False):
    """Aggregate original applications minus explicit returns within the ledger snapshot."""
    if not identifiers:
        return {}
    source, returned = BusinessAllocationLine, BusinessAllocationReturnLine
    if by_document_line:
        identity, amount = source.document_line_id, source.document_amount
        negative = select(identity.label("identity"), (-returned.document_amount).label("amount"))
        negative = negative.join(
            returned,
            (returned.source_allocation_line_id == source.id)
            & (returned.ledger_id == source.ledger_id),
        ).where(returned.ledger_id == ledger_id)
        negative_scope = source.ledger_id
    else:
        identity, amount = source.payment_id, source.payment_amount
        negative = select(
            returned.payment_id.label("identity"), (-returned.payment_amount).label("amount")
        )
        negative_scope = returned.ledger_id
    positive = select(identity.label("identity"), amount.label("amount")).where(
        source.ledger_id == ledger_id, identity.in_(identifiers)
    )
    negative_identity = identity if by_document_line else returned.payment_id
    negative = negative.where(negative_scope == ledger_id, negative_identity.in_(identifiers))
    entries = union_all(positive, negative).subquery()
    return dict(
        session.execute(
            select(entries.c.identity, func.sum(entries.c.amount)).group_by(entries.c.identity)
        ).all()
    )
