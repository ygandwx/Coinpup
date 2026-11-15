"""Exact net applications in one query, without multiplying joined source rows."""

from sqlalchemy import Numeric, Uuid, cast, column, func, literal, select, union_all
from sqlalchemy.dialects.postgresql import ARRAY

from coinpup_api.business.allocation_return_projections import current_return_members
from coinpup_api.business.credit_projections import current_credit_lines
from coinpup_api.business.settlement_models import BusinessAllocationLine


def _applications(ledger_id, identifiers, *, by_document_line):
    source = BusinessAllocationLine
    identity = source.document_line_id if by_document_line else source.payment_id
    amount = source.document_amount if by_document_line else source.payment_amount
    scoped = (
        select(source.id, identity.label("identity"), amount.label("amount"))
        .where(source.ledger_id == ledger_id, identity.in_(identifiers))
        .cte("scoped_applications")
    )
    requested = select(func.array_agg(scoped.c.id)).correlate(None).scalar_subquery()
    live = current_return_members(ledger_id, requested)
    returned = live.c.document_amount if by_document_line else live.c.payment_amount
    debits = select(scoped.c.identity, scoped.c.amount)
    credits = select(scoped.c.identity, -returned).select_from(
        scoped.join(live, live.c.source_allocation_line_id == scoped.c.id)
    )
    return union_all(debits, credits).add_cte(scoped, nest_here=True).subquery()


def document_totals(session, ledger_id, identifiers):
    """Keep net settlements and manual credit components separate in one bounded query."""
    identifiers = list(identifiers or ())
    if not identifiers:
        return {}
    apps = _applications(ledger_id, identifiers, by_document_line=True).c
    credit = current_credit_lines(ledger_id, identifiers).c
    amounts = select(
        apps.identity,
        apps.amount.label("applied"),
        literal(0).label("credited_net"),
        literal(0).label("credited_tax"),
    )
    credits = select(credit.source_line_id, literal(0), credit.net_amount, credit.tax_amount)
    entries = union_all(amounts, credits).subquery()
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
    identifiers = list(identifiers or ())
    if not identifiers:
        return {}
    apps = _applications(ledger_id, identifiers, by_document_line=False).c
    principals = func.public.coinpup_current_refund_principals(
        cast(ledger_id, Uuid), cast(identifiers, ARRAY(Uuid))
    ).table_valued(column("payment_id", Uuid), column("payment_amount", Numeric(38, 18)))
    amounts = select(apps.identity, apps.amount.label("applied"), literal(0).label("refunded"))
    refunds = select(principals.c.payment_id, literal(0), principals.c.payment_amount)
    entries = union_all(amounts, refunds).subquery()
    return {
        identifier: (applied, refunded)
        for identifier, applied, refunded in session.execute(
            select(
                entries.c.identity, func.sum(entries.c.applied), func.sum(entries.c.refunded)
            ).group_by(entries.c.identity)
        )
    }


def applied_totals(session, ledger_id, identifiers, *, by_document_line=False):
    """Read net applications for the requested payment or document line identities."""
    identifiers = list(identifiers or ())
    if not identifiers:
        return {}
    apps = _applications(ledger_id, identifiers, by_document_line=by_document_line).c
    return dict(
        session.execute(select(apps.identity, func.sum(apps.amount)).group_by(apps.identity)).all()
    )
