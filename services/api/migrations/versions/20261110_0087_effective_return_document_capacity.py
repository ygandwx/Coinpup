"""Check the resulting applications against each current document in its own asset."""

from pathlib import Path

from alembic import op

revision = "20261110_0087"
down_revision = "20261110_0086"
branch_labels = None
depends_on = None

_CHECK = """
CREATE FUNCTION coinpup_return_document_capacity_ok(ledger_identifier uuid,source_ids uuid[],
    deltas numeric[]) RETURNS boolean
LANGUAGE sql STABLE SET search_path=pg_catalog AS $$
WITH changes AS MATERIALIZED (
    SELECT id,sum(coalesce(delta,0)) AS delta FROM unnest(source_ids,deltas) v(id,delta)
    GROUP BY id
), targets AS MATERIALIZED (
    SELECT a.id,a.document_line_id FROM public.business_allocation_lines a
    JOIN changes ch ON ch.id=a.id WHERE a.ledger_id=ledger_identifier
), allocations AS MATERIALIZED (
    SELECT a.id,a.document_line_id,a.document_amount,g.allocation_date
    FROM public.business_allocation_lines a JOIN public.business_allocations g ON
        (g.id,g.ledger_id)=(a.allocation_id,a.ledger_id)
    WHERE a.ledger_id=ledger_identifier AND a.document_line_id IN
        (SELECT document_line_id FROM targets)
), pinned AS MATERIALIZED (
    SELECT f.* FROM public.coinpup_allocation_document_facts(ledger_identifier,NULL,
        ARRAY(SELECT id FROM allocations)) f
), current_lines AS MATERIALIZED (
    SELECT e.* FROM public.coinpup_effective_document_lines(ledger_identifier,NULL,
        ARRAY(SELECT DISTINCT document_line_id FROM targets)) e
), returned AS (
    SELECT r.source_allocation_line_id,sum(r.document_amount) AS amount
    FROM public.coinpup_current_return_members(ledger_identifier,
        ARRAY(SELECT id FROM allocations)) r
    GROUP BY r.source_allocation_line_id
), exposures AS MATERIALIZED (
    SELECT a.*,a.document_amount-coalesce(r.amount,0)+coalesce(ch.delta,0) AS amount
    FROM allocations a LEFT JOIN returned r ON r.source_allocation_line_id=a.id
    LEFT JOIN changes ch ON ch.id=a.id
), totals AS (
    SELECT document_line_id,sum(amount) AS amount FROM exposures GROUP BY document_line_id
), credits AS (
    SELECT c.source_line_id,sum(c.net_amount) AS net,sum(c.tax_amount) AS tax,
        sum(c.total_amount) AS gross FROM public.coinpup_current_credit_lines(
            ledger_identifier,ARRAY(SELECT document_line_id FROM totals)) c
    GROUP BY c.source_line_id
)
SELECT coalesce(cardinality(source_ids),0) BETWEEN 1 AND 400
    AND (deltas IS NULL OR (cardinality(source_ids)=cardinality(deltas)
        AND array_position(deltas,NULL::numeric) IS NULL))
    AND (SELECT count(*) FROM targets)=(SELECT count(*) FROM changes)
    AND NOT EXISTS (
        SELECT 1 FROM exposures a LEFT JOIN pinned p ON p.allocation_line_id=a.id
        LEFT JOIN current_lines e ON e.source_line_id=a.document_line_id
        WHERE a.amount<0 OR a.amount>a.document_amount OR p.allocation_line_id IS NULL
            OR (a.amount>0 AND (e.source_line_id IS NULL OR a.allocation_date<e.issue_date
                OR (p.document_id,p.asset_id,p.party_id,p.project_id) IS DISTINCT FROM
                   (e.document_id,e.asset_id,e.party_id,e.project_id)))
    ) AND NOT EXISTS (
        SELECT 1 FROM totals t LEFT JOIN current_lines e ON e.source_line_id=t.document_line_id
        LEFT JOIN credits c ON c.source_line_id=t.document_line_id
        WHERE t.amount+coalesce(c.gross,0)>coalesce(e.total_amount,0)
            OR coalesce(c.net,0)>coalesce(e.net_amount,0)
            OR coalesce(c.tax,0)>coalesce(e.tax_amount,0)
    );
$$;
"""

# Preserve the original payment and owned-source checks, including effective refund principal.
_OLD = (Path(__file__).resolve().parent / "sql/0087/return_capacity.sql").read_text(
    encoding="utf-8"
)
_FROM = _OLD.index("    SELECT array_agg(DISTINCT a.document_line_id)")
_TO = _OLD.index("        RAISE EXCEPTION", _FROM)
_CAPACITY = (
    _OLD[:_FROM].replace(" documents uuid[];", "")
    + """    IF NOT public.coinpup_return_document_capacity_ok(
        ledger_identifier,source_identifiers,NULL) THEN
"""
    + _OLD[_TO:]
)


def upgrade():
    op.execute("SELECT pg_advisory_xact_lock(18945999704708432)")
    op.execute(_CHECK)
    op.execute(_CAPACITY)


def downgrade():
    op.execute("SELECT pg_advisory_xact_lock(18945999704708432)")
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM public.business_document_revisions) THEN
                RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='Revised document capacity exists',
                    CONSTRAINT='ck_return_document_capacity_downgrade';
            END IF;
        END; $$;
    """)
    op.execute(_OLD)
    op.execute("DROP FUNCTION coinpup_return_document_capacity_ok(uuid,uuid[],numeric[])")
