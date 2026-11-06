"""Typed, ledger-bounded canonical history and current credit projections."""

from sqlalchemy import Date, Integer, Numeric, Text, Uuid, cast, column, func, select
from sqlalchemy.dialects.postgresql import ARRAY

from coinpup_api.business.credit_receipts import integrity

_UUIDS = (
    "ledger_id",
    "credit_id",
    "document_id",
    "revision_id",
    "source_line_id",
    "source_operation_id",
    "operation_id",
    "journal_id",
)


def _relation(function, ledger_id, *identifiers):
    return function(
        cast(ledger_id, Uuid), *(cast(values, ARRAY(Uuid)) for values in identifiers)
    ).table_valued(
        *(column(name, Uuid) for name in _UUIDS),
        *(column(name, Integer) for name in ("group_version", "line_no", "operation_version")),
        *(column(name, Numeric(38, 18)) for name in ("net_amount", "tax_amount", "total_amount")),
        *(column(name, Date) for name in ("recognition_date", "credit_date")),
        *(column(name, Text) for name in ("credit_reason", "posting_revision_reason")),
    )


def posting_facts(ledger_id, *, credit_ids=None, journal_ids=None):
    """At least one explicit identifier array is required; empty arrays produce no rows."""
    return _relation(func.public.coinpup_credit_posting_facts, ledger_id, credit_ids, journal_ids)


def current_credit_lines(ledger_id, source_ids):
    return _relation(func.public.coinpup_current_credit_lines, ledger_id, source_ids)


def posting_fact(session, ledger_id, journal_id):
    """A history lookup never silently chooses between duplicate cross-table identities."""
    rows = (
        session.execute(select(posting_facts(ledger_id, journal_ids=[journal_id]))).mappings().all()
    )
    if len(rows) != 1:
        raise integrity()
    return rows[0]
