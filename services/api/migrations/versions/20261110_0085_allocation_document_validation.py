"""Validate historical allocation journals and effective confirmed document capacity separately."""

from pathlib import Path

from alembic import op

revision = "20261110_0085"
down_revision = "20261110_0084"
branch_labels = None
depends_on = None

# Exact PostgreSQL 17 definition at 0084, including current return and refund quantities.
_OLD = (Path(__file__).resolve().parent / "sql/0085/allocation.sql").read_text(encoding="utf-8")
_ALLOCATION = (
    _OLD.replace(
        "FOR item IN WITH return_sources AS MATERIALIZED (",
        """FOR item IN WITH pinned AS MATERIALIZED (
        SELECT p.* FROM public.coinpup_allocation_document_facts(
            allocation.ledger_id,allocation.id,NULL) p
    ), effective AS MATERIALIZED (
        SELECT e.* FROM public.coinpup_effective_document_lines(
            allocation.ledger_id,NULL,ARRAY(SELECT source_line_id FROM pinned)) e
    ), return_sources AS MATERIALIZED (""",
    )
    .replace(
        "d.state, d.document_kind, d.party_id, d.issue_date,\n"
        "        d.asset_id AS document_asset, s.project_id, s.description, s.total_amount,\n"
        "        s.id AS source_id",
        "d.state, s.document_kind, s.party_id, s.issue_date,\n"
        "        s.asset_id AS document_asset, s.project_id, s.description, s.total_amount,\n"
        "        coalesce(e.total_amount,0) AS current_capacity, s.source_line_id AS source_id",
    )
    .replace(
        """LEFT JOIN public.business_document_lines s ON s.id=l.document_line_id
            AND s.document_id=d.id AND s.ledger_id=l.ledger_id""",
        """LEFT JOIN pinned s ON (s.allocation_line_id,s.ledger_id)=(l.id,l.ledger_id)
        LEFT JOIN effective e ON (e.source_line_id,e.document_id,e.ledger_id)=
            (l.document_line_id,l.document_id,l.ledger_id)""",
    )
    .replace("da.asset_id=d.asset_id", "da.asset_id=s.asset_id")
    .replace(
        "+ coalesce(item.current_credit_total,0) > item.total_amount",
        "+ coalesce(item.current_credit_total,0) > item.current_capacity",
    )
    .replace(
        """OR NOT EXISTS (SELECT 1 FROM public.business_postings
               WHERE line_id=item.document_line_id AND document_id=item.document_id
                   AND ledger_id=allocation.ledger_id)""",
        """OR (item.document_revision_line_id IS NULL AND NOT EXISTS (
               SELECT 1 FROM public.business_postings
               WHERE (line_id,document_id,ledger_id)=
                   (item.document_line_id,item.document_id,allocation.ledger_id)))
           OR (item.document_revision_line_id IS NOT NULL AND NOT EXISTS (
               SELECT 1 FROM public.business_document_accruals a
               WHERE (a.replacement_fact_id,a.source_line_id,a.document_id,a.ledger_id)=
                   (item.document_revision_line_id,item.document_line_id,item.document_id,
                    allocation.ledger_id) AND a.journal_id IS NOT NULL))""",
    )
)


def upgrade():
    op.execute("SELECT pg_advisory_xact_lock(18945999704708432)")
    op.execute(_ALLOCATION)


def downgrade():
    op.execute("SELECT pg_advisory_xact_lock(18945999704708432)")
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM public.business_document_revisions) THEN
                RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='Versioned document capacity exists',
                    CONSTRAINT='ck_allocation_document_validation_downgrade';
            END IF;
        END; $$;
    """)
    op.execute(_OLD)
