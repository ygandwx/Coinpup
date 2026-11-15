"""Typed, ledger-scoped historical posting snapshots for allocation return receipts."""

from sqlalchemy import BigInteger, Date, Integer, Numeric, Text, Uuid, cast, column, func, select
from sqlalchemy.dialects.postgresql import ARRAY

from coinpup_api.business.allocation_return_receipts import integrity


def return_posting_facts(ledger_id, *, return_ids=None, journal_ids=None):
    return func.public.coinpup_return_posting_facts(
        cast(ledger_id, Uuid), cast(return_ids, ARRAY(Uuid)), cast(journal_ids, ARRAY(Uuid))
    ).table_valued(
        *(
            column(name, Uuid)
            for name in (
                "ledger_id",
                "return_id",
                "payment_id",
                "revision_id",
                "source_allocation_line_id",
                "source_operation_id",
                "operation_id",
                "journal_id",
                "created_by",
                "party_id",
                "document_id",
                "document_line_id",
                "project_id",
            )
        ),
        *(
            column(name, Integer)
            for name in ("group_version", "source_version", "line_no", "operation_version")
        ),
        column("source_return_no", BigInteger),
        column("return_date", Date),
        *(column(name, Numeric(38, 18)) for name in ("payment_amount", "document_amount")),
        *(
            column(name, Text)
            for name in (
                "return_reason",
                "revision_reason",
                "direction",
                "payment_asset_id",
                "document_asset_id",
            )
        ),
    )


def return_posting_fact(session, ledger_id, journal_id):
    facts = return_posting_facts(ledger_id, journal_ids=[journal_id])
    rows = session.execute(select(facts)).mappings().all()
    if len(rows) != 1:
        raise integrity()
    return rows[0]
