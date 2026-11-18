"""Recheck every effective dependency after a staged whole-document change."""

from alembic import op

revision = "20261113_0094"
down_revision = "20261113_0093"
branch_labels = None
depends_on = None

_PROOF = """
CREATE FUNCTION coinpup_validate_document_dependencies(ledger_identifier uuid,
    document_identifier uuid) RETURNS void
LANGUAGE plpgsql SET search_path=pg_catalog AS $$
DECLARE source_ids uuid[]; batch uuid[];
BEGIN
    IF NOT EXISTS (SELECT 1 FROM public.business_documents d
        JOIN public.business_confirmations c ON (c.document_id,c.ledger_id)=(d.id,d.ledger_id)
        WHERE (d.id,d.ledger_id,d.state)=(document_identifier,ledger_identifier,'confirmed')) THEN
        RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='Document dependency scope is invalid',
            CONSTRAINT='ck_document_dependency_scope';
    END IF;
    -- Include removed and historical identities: omitted sources have zero current capacity.
    SELECT array_agg(s.id ORDER BY s.id) INTO source_ids
    FROM public.business_document_lines s
    WHERE (s.document_id,s.ledger_id)=(document_identifier,ledger_identifier);
    IF public.coinpup_credit_document_capacity_ok(
        ledger_identifier,document_identifier,source_ids) IS NOT TRUE THEN
        RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='Effective document capacity is exceeded',
            CONSTRAINT='ck_document_dependency_capacity';
    END IF;
    -- One representative per source selects ALL its historical applications and current
    -- returns. Bound each helper input, without bounding the document's historical row count.
    FOR batch IN WITH representatives AS (
        SELECT DISTINCT ON (a.document_line_id) a.id,a.document_line_id
        FROM public.business_allocation_lines a
        WHERE (a.document_id,a.ledger_id)=(document_identifier,ledger_identifier)
        ORDER BY a.document_line_id,a.id
    ), numbered AS (
        SELECT id,row_number() OVER (ORDER BY document_line_id) AS position FROM representatives
    ) SELECT array_agg(id ORDER BY position) FROM numbered GROUP BY (position-1)/400 LOOP
        IF public.coinpup_return_document_capacity_ok(ledger_identifier,batch,NULL) IS NOT TRUE THEN
            RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='Effective document sources differ',
                CONSTRAINT='ck_document_dependency_sources';
        END IF;
    END LOOP;
END; $$;
"""


def upgrade():
    op.execute("SELECT pg_advisory_xact_lock(18945999704708432)")
    op.execute(_PROOF)


def downgrade():
    op.execute("SELECT pg_advisory_xact_lock(18945999704708432)")
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM public.business_document_revisions) THEN
                RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='Document dependencies exist',
                    CONSTRAINT='ck_document_dependency_downgrade';
            END IF;
        END; $$;
    """)
    op.execute("DROP FUNCTION coinpup_validate_document_dependencies(uuid,uuid)")
