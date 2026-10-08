"""Read the current period inside the already serialized financial transaction."""

from sqlalchemy import select

from coinpup_api.ledger.period_models import LedgerPeriod
from coinpup_api.ledger.service import LedgerError


def require_open_period(session, ledger_id, *dated_records):
    cutoff = session.scalar(
        select(LedgerPeriod.closed_through).where(LedgerPeriod.ledger_id == ledger_id)
    )
    if cutoff is not None and any(
        record.transaction_date <= cutoff
        or getattr(record, "recognition_date", record.transaction_date) <= cutoff
        for record in dated_records
    ):
        raise LedgerError("period_closed", 409, "Reopen the closed period before posting.")
