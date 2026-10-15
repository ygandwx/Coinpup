"""Exact net applications in one query, without multiplying joined source rows."""

from sqlalchemy import func, select, union_all

from coinpup_api.business.allocation_return_models import BusinessAllocationReturnLine
from coinpup_api.business.settlement_models import BusinessAllocationLine


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
