"""Bound current return quantities and permanent source positions to one ledger and source set."""

from sqlalchemy import BigInteger, Numeric, Uuid, cast, column, func, literal, select, union_all
from sqlalchemy.dialects.postgresql import ARRAY

from coinpup_api.business.allocation_return_models import BusinessAllocationReturnLine
from coinpup_api.business.allocation_return_revision_member_models import (
    BusinessReturnRevisionMember,
)


def current_return_members(ledger_id, source_ids):
    return func.public.coinpup_current_return_members(
        cast(ledger_id, Uuid), cast(source_ids, ARRAY(Uuid))
    ).table_valued(
        column("source_allocation_line_id", Uuid),
        column("payment_amount", Numeric(38, 18)),
        column("document_amount", Numeric(38, 18)),
    )


def return_source_state(session, ledger_id, source_ids):
    """Sum only live shares; the highest reserved source slot survives cancellation."""
    source_ids = list(source_ids or ())
    if not source_ids:
        return {}
    current = current_return_members(ledger_id, source_ids).c
    original, revision = BusinessAllocationReturnLine, BusinessReturnRevisionMember
    live = select(
        current.source_allocation_line_id.label("source_id"),
        current.payment_amount,
        current.document_amount,
        cast(0, BigInteger).label("position"),
    )

    def positioned(table):
        return select(
            table.source_allocation_line_id, literal(0), literal(0), table.source_return_no
        ).where(table.ledger_id == ledger_id, table.source_allocation_line_id.in_(source_ids))

    # Corrections retain their original slot. Only a create transition reserves a new one.
    facts = union_all(
        live, positioned(original), positioned(revision).where(revision.action == "create")
    ).subquery()
    return {
        source: (payment, document, position)
        for source, payment, document, position in session.execute(
            select(
                facts.c.source_id,
                func.sum(facts.c.payment_amount),
                func.sum(facts.c.document_amount),
                func.max(facts.c.position),
            ).group_by(facts.c.source_id)
        )
    }
