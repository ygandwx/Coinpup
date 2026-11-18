"""Read the effective confirmed source facts for one document or an explicit list of lines."""

from sqlalchemy import Date, Integer, Numeric, String, Uuid, cast, column, func
from sqlalchemy.dialects.postgresql import ARRAY


def effective_document_lines(ledger_id, *, document_id=None, line_ids=None):
    return func.public.coinpup_effective_document_lines(
        cast(ledger_id, Uuid),
        cast(document_id, Uuid),
        cast(None if line_ids is None else list(line_ids), ARRAY(Uuid)),
    ).table_valued(
        column("ledger_id", Uuid),
        column("confirmation_id", Uuid),
        column("document_id", Uuid),
        column("document_version", Integer),
        column("document_kind", String),
        column("party_id", Uuid),
        column("asset_id", String),
        column("issue_date", Date),
        column("source_line_id", Uuid),
        column("line_no", Integer),
        column("description", String),
        column("category_id", Uuid),
        column("category_kind", String),
        column("project_id", Uuid),
        column("recognition_date", Date),
        column("net_amount", Numeric),
        column("tax_amount", Numeric),
        column("total_amount", Numeric),
        column("operation_id", Uuid),
        column("operation_version", Integer),
        column("journal_id", Uuid),
    )
