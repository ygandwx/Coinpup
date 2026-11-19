"""Batch effective credit sources within the caller's locked write transaction."""

from decimal import Decimal
from types import SimpleNamespace

from sqlalchemy import select

from coinpup_api.ledger.models import FinancialOperation
from coinpup_api.ledger.service import LedgerError, _not_found


def current_credit_sources(session, ledger_id, current, requested):
    """Keep source identity, exact priced facts and the current sealed accrual together."""
    if current.document is None or current.document.ledger_id != ledger_id:
        raise _not_found()
    identifiers = {row.source_line_id for row in requested.lines}
    postings = {p.source_line_id: p for p in current.postings if p.source_line_id in identifiers}
    operations = {
        op.id: op
        for op in session.scalars(
            select(FinancialOperation).where(
                FinancialOperation.ledger_id == ledger_id,
                FinancialOperation.id.in_([p.operation_id for p in postings.values()]),
            )
        )
    }
    sources = {}
    for row in current.document.lines:
        posting = postings.get(row.id)
        if posting is None or posting.operation_id not in operations:
            continue
        operation = operations[posting.operation_id]
        if operation.version != posting.version:
            raise LedgerError("version_conflict", 409, "The source operation version changed.")
        if (
            operation.kind != f"{current.document.document_kind}_accrual"
            or operation.status != "active"
            or operation.current_journal_id != posting.journal_id
        ):
            raise LedgerError("settlement_inactive", 409, "The source accrual is not active.")
        # Receipt quantities are exact strings with database padding, never floats.
        fact = SimpleNamespace(
            **row.model_dump(exclude={"net_amount", "tax_amount", "total_amount"}),
            **{
                name: Decimal(getattr(row, name))
                for name in ("net_amount", "tax_amount", "total_amount")
            },
        )
        sources[row.id] = (fact, posting, operation)
    if sources.keys() != identifiers:
        raise _not_found()
    return sources
