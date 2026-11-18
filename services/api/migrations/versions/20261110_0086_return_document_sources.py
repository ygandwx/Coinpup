"""Resolve allocation returns, their fractional shares and their receipts from pinned facts."""

from pathlib import Path

from alembic import op

revision = "20261110_0086"
down_revision = "20261110_0085"
branch_labels = None
depends_on = None

_FROZEN = Path(__file__).resolve().parent / "sql/0086"
_OLD = {
    name: (_FROZEN / f"{name}.sql").read_text(encoding="utf-8")
    for name in ("posting", "sequence", "history", "receipt")
}
# Use one scoped set for every allocation in a return group or historical prefix.
_POSTING = (
    _OLD["posting"]
    .replace(
        "    )\n    SELECT f.ledger_id, f.return_id, f.payment_id, f.revision_id, f.group_version,",
        """    ), pinned AS MATERIALIZED (
        SELECT p.* FROM public.coinpup_allocation_document_facts(ledger_identifier,NULL,
            ARRAY(SELECT DISTINCT source_allocation_line_id FROM facts)) p
    )
    SELECT f.ledger_id, f.return_id, f.payment_id, f.revision_id, f.group_version,""",
    )
    .replace("d.asset_id::text, l.project_id", "d.asset_id::text, d.project_id")
    .replace(
        """JOIN public.business_documents d ON (d.id,d.ledger_id)=(a.document_id,a.ledger_id)
    JOIN public.business_document_lines l
      ON (l.id,l.document_id,l.ledger_id)=(a.document_line_id,a.document_id,a.ledger_id)""",
        """JOIN pinned d ON (d.allocation_line_id,d.ledger_id)=(a.id,a.ledger_id)""",
    )
)
_DOCUMENT = "JOIN public.business_documents d ON (d.id,d.ledger_id)=(a.document_id,a.ledger_id)"
_PINNED = (
    "JOIN public.coinpup_allocation_document_facts(ledger_identifier,NULL,{ids}) d "
    "ON (d.allocation_line_id,d.ledger_id)=(a.id,a.ledger_id)"
)
_SEQUENCE = _OLD["sequence"].replace(_DOCUMENT, _PINNED.format(ids="source_identifiers"))
_HISTORY = _OLD["history"].replace(_DOCUMENT, _PINNED.format(ids="sources"))
_RECEIPT = (
    _OLD["receipt"]
    .replace("OR a.id IS NULL OR d.id IS NULL", "OR a.id IS NULL OR d.allocation_line_id IS NULL")
    .replace(
        "LEFT JOIN public.business_documents d ON (d.id,d.ledger_id)=(a.document_id,h.ledger_id)",
        """LEFT JOIN public.coinpup_allocation_document_facts(h.ledger_id,NULL,
            ARRAY(SELECT DISTINCT source_allocation_line_id FROM members)) d
          ON (d.allocation_line_id,d.ledger_id)=(a.id,h.ledger_id)""",
    )
)


def upgrade():
    op.execute("SELECT pg_advisory_xact_lock(18945999704708432)")
    for statement in (_POSTING, _SEQUENCE, _HISTORY, _RECEIPT):
        op.execute(statement)


def downgrade():
    op.execute("SELECT pg_advisory_xact_lock(18945999704708432)")
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM public.business_allocation_lines
                WHERE document_revision_line_id IS NOT NULL) THEN
                RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='Versioned return source exists',
                    CONSTRAINT='ck_return_document_source_downgrade';
            END IF;
        END; $$;
    """)
    for statement in _OLD.values():
        op.execute(statement)
