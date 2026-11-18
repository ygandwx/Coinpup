"""Read the immutable document source pinned to a specific allocation, within its ledger."""

from sqlalchemy import Date, Integer, Numeric, String, Uuid, cast, column, func
from sqlalchemy.dialects.postgresql import ARRAY


def allocation_document_facts(ledger_id, *, allocation_id=None, line_ids=None):
    return func.public.coinpup_allocation_document_facts(
        cast(ledger_id, Uuid),
        cast(allocation_id, Uuid),
        cast(None if line_ids is None else list(line_ids), ARRAY(Uuid)),
    ).table_valued(
        *(
            column(name, Uuid)
            for name in (
                "allocation_line_id",
                "allocation_id",
                "ledger_id",
                "document_id",
                "source_line_id",
                "party_id",
                "project_id",
                "category_id",
            )
        ),
        column("document_version", Integer),
        column("document_kind", String),
        column("asset_id", String),
        column("issue_date", Date),
        column("description", String),
        column("recognition_date", Date),
        column("net_amount", Numeric),
        column("tax_amount", Numeric),
        column("total_amount", Numeric),
    )
