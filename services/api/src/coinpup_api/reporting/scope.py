"""Validate an explicit selection and verify every ledger in the same owner snapshot."""

from uuid import UUID

from sqlalchemy import select

from coinpup_api.ledger.models import Ledger
from coinpup_api.ledger.service import LedgerError, _not_found


def report_scope(ledgers):
    if (
        not isinstance(ledgers, (list, tuple))
        or not 1 <= len(ledgers) <= 100
        or any(not isinstance(item, UUID) for item in ledgers)
        or len(set(ledgers)) != len(ledgers)
    ):
        raise LedgerError("invalid_report_scope", 422, "Select distinct ledgers for this report.")
    return tuple(ledgers)


def require_owned_ledgers(session, owner, scope):
    owned = set(
        session.scalars(select(Ledger.id).where(Ledger.owner_id == owner, Ledger.id.in_(scope)))
    )
    if owned != set(scope):
        raise _not_found()
