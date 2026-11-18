"""Validate active credits against current prices while retaining their immutable source proof."""

from pathlib import Path

from alembic import op

revision = "20261113_0091"
down_revision = "20261113_0090"
branch_labels = None
depends_on = None

_OLD = (Path(__file__).resolve().parent / "sql/0091/credit.sql").read_text(encoding="utf-8")
_CHECK = """
CREATE OR REPLACE FUNCTION public.coinpup_credit_document_capacity_ok(
    ledger_identifier uuid,document_identifier uuid,source_ids uuid[]) RETURNS boolean
LANGUAGE sql STABLE SET search_path=pg_catalog AS $$
WITH targets AS MATERIALIZED (
    SELECT s.id FROM public.business_document_lines s WHERE
        (s.ledger_id,s.document_id)=(ledger_identifier,document_identifier)
        AND s.id=ANY(source_ids)
), effective AS MATERIALIZED (
    SELECT e.* FROM public.coinpup_effective_document_lines(
        ledger_identifier,document_identifier,source_ids) e
), credits AS MATERIALIZED (
    SELECT c.* FROM public.coinpup_current_credit_lines(ledger_identifier,source_ids) c
), pinned AS MATERIALIZED (
    SELECT p.* FROM public.coinpup_credit_document_sources(ledger_identifier,NULL,
        ARRAY(SELECT journal_id FROM credits)) p
), totals AS (
    SELECT source_line_id,sum(net_amount) AS net,sum(tax_amount) AS tax,
        sum(total_amount) AS gross FROM credits GROUP BY source_line_id
), allocations AS MATERIALIZED (
    SELECT a.id,a.document_line_id,a.document_amount FROM public.business_allocation_lines a
    WHERE a.ledger_id=ledger_identifier AND a.document_line_id=ANY(source_ids)
), applied AS (
    SELECT document_line_id,sum(document_amount) AS amount FROM allocations
    GROUP BY document_line_id
), returned AS (
    SELECT a.document_line_id,sum(r.document_amount) AS amount
    FROM public.coinpup_current_return_members(ledger_identifier,
        ARRAY(SELECT id FROM allocations)) r
    JOIN allocations a ON a.id=r.source_allocation_line_id GROUP BY a.document_line_id
)
SELECT coalesce(cardinality(source_ids),0)>0 AND array_position(source_ids,NULL::uuid) IS NULL
    AND (SELECT count(*) FROM targets)=(SELECT count(DISTINCT id) FROM unnest(source_ids) s(id))
    AND NOT EXISTS (
        SELECT 1 FROM credits c LEFT JOIN pinned p ON p.journal_id=c.journal_id
        LEFT JOIN effective e ON e.source_line_id=c.source_line_id
        WHERE p.journal_id IS NULL OR e.source_line_id IS NULL OR c.credit_date<e.issue_date
            OR (p.document_id,p.asset_id,p.party_id,p.project_id,p.category_id,
                c.source_operation_id) IS DISTINCT FROM
               (e.document_id,e.asset_id,e.party_id,e.project_id,e.category_id,e.operation_id)
    ) AND NOT EXISTS (
        SELECT 1 FROM targets s LEFT JOIN effective e ON e.source_line_id=s.id
        LEFT JOIN totals c ON c.source_line_id=s.id
        LEFT JOIN applied a ON a.document_line_id=s.id
        LEFT JOIN returned r ON r.document_line_id=s.id
        WHERE coalesce(a.amount,0)-coalesce(r.amount,0)<0
            OR coalesce(c.net,0)>coalesce(e.net_amount,0)
            OR coalesce(c.tax,0)>coalesce(e.tax_amount,0)
            OR coalesce(c.gross,0)+coalesce(a.amount,0)-coalesce(r.amount,0)
                >coalesce(e.total_amount,0)
    );
$$;
"""
_FROM = _OLD.index("    FOR item IN WITH return_sources")
_TO = _OLD.index("            RAISE EXCEPTION", _FROM)
_VALIDATE = (
    _OLD[:_FROM]
    + """    IF NOT public.coinpup_credit_document_capacity_ok(
        entry.ledger_id,entry.document_id,source_ids) THEN
"""
    + _OLD[_TO:].replace("    END LOOP;\nEND;", "END;")
)


def upgrade():
    op.execute("SELECT pg_advisory_xact_lock(18945999704708432)")
    op.execute(_CHECK)
    op.execute(_VALIDATE)


def downgrade():
    op.execute("SELECT pg_advisory_xact_lock(18945999704708432)")
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM public.business_document_revisions) THEN
                RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='Revised credit capacity exists',
                    CONSTRAINT='ck_credit_document_capacity_downgrade';
            END IF;
        END; $$;
    """)
    op.execute(_OLD)
    op.execute("DROP FUNCTION public.coinpup_credit_document_capacity_ok(uuid,uuid,uuid[])")
