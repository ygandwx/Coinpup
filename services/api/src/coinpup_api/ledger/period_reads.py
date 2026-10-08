"""Consistent optional-period metadata; future reports retain their own snapshots."""

from datetime import date

from sqlalchemy import func, or_, select

from coinpup_api.ledger.period_models import LedgerPeriod, PeriodAudit
from coinpup_api.ledger.period_schemas import PeriodChangeResponse, PeriodState
from coinpup_api.ledger.service import LedgerError, LedgerService, _page


class PeriodReads(LedgerService):
    def state(self, owner_id, ledger_id, *, from_date=None, to_date=None, date_basis="transaction"):
        if (
            date_basis not in {"transaction", "recognition"}
            or (from_date is None) != (to_date is None)
            or (
                from_date is not None
                and (
                    type(from_date) is not date or type(to_date) is not date or from_date > to_date
                )
            )
        ):
            raise LedgerError("invalid_date_range", 422, "Use a complete ordered calendar range.")
        with self._transaction(owner_id, read_only=True) as session:
            self._ledger(session, owner_id, ledger_id)
            state = session.scalar(select(LedgerPeriod).where(LedgerPeriod.ledger_id == ledger_id))
            cutoff = state.closed_through if state else None
            reopened = select(PeriodAudit).where(
                PeriodAudit.ledger_id == ledger_id, PeriodAudit.action == "reopen"
            )
            if from_date is not None:
                reopened = reopened.where(
                    PeriodAudit.previous_closed_through >= from_date,
                    or_(PeriodAudit.closed_through.is_(None), PeriodAudit.closed_through < to_date),
                )
            audit = session.scalar(reopened.order_by(PeriodAudit.version.desc()).limit(1))
            status = None
            if from_date is not None:
                status = (
                    "open"
                    if cutoff is None or cutoff < from_date
                    else "closed"
                    if cutoff >= to_date
                    else "partial"
                )
            return PeriodState(
                ledger_id=ledger_id,
                version=state.version if state else 1,
                closed_through=cutoff,
                from_date=from_date,
                to_date=to_date,
                date_basis=date_basis,
                range_status=status,
                generated_at=session.scalar(select(func.clock_timestamp())),
                last_reopened=PeriodChangeResponse.model_validate(audit) if audit else None,
            )

    def history(self, owner_id, ledger_id, *, limit=100, offset=0):
        _page(limit, offset)
        with self._transaction(owner_id, read_only=True) as session:
            self._ledger(session, owner_id, ledger_id)
            return [
                PeriodChangeResponse.model_validate(row)
                for row in session.scalars(
                    select(PeriodAudit)
                    .where(PeriodAudit.ledger_id == ledger_id)
                    .order_by(PeriodAudit.version.desc())
                    .limit(limit)
                    .offset(offset)
                )
            ]
