"""Read current return versions for a refund after the ledger write lock has been acquired."""

from uuid import UUID

from sqlalchemy import Date, Integer, Uuid, cast, column, func, select
from sqlalchemy.dialects.postgresql import ARRAY

from coinpup_api.business.allocation_return_models import BusinessAllocationReturn
from coinpup_api.business.refund_plan import RefundReturnSource
from coinpup_api.ledger.service import LedgerError, _not_found


def bound_return_sources(
    session, ledger_id, payment_id, identifiers
) -> dict[UUID, RefundReturnSource]:
    if not identifiers:
        return {}
    origin = BusinessAllocationReturn
    active = func.public.coinpup_current_return_evidence(
        cast(ledger_id, Uuid), cast(payment_id, Uuid), cast(list(identifiers), ARRAY(Uuid))
    ).table_valued(
        column("return_id", Uuid), column("return_version", Integer), column("return_date", Date)
    )
    rows = session.execute(
        select(origin.id, active.c.return_version, active.c.return_date)
        .outerjoin(active, active.c.return_id == origin.id)
        .where(
            origin.ledger_id == ledger_id,
            origin.payment_id == payment_id,
            origin.id.in_(identifiers),
        )
    ).all()
    if len(rows) != len(identifiers):
        raise _not_found()
    if any(row.return_version is None for row in rows):
        raise LedgerError(
            "operation_cancelled", 409, "A referenced allocation return is cancelled."
        )
    return {
        row.id: RefundReturnSource(
            row.id, ledger_id, payment_id, row.return_date, row.return_version
        )
        for row in rows
    }
