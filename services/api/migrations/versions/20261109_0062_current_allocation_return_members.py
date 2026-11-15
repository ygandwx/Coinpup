"""Project only the latest active return members of the requested original application lines."""

from alembic import op

revision = "20261109_0062"
down_revision = "20261109_0061"
branch_labels = None
depends_on = None

_CURRENT = r"""
CREATE FUNCTION coinpup_current_return_members(
    ledger_identifier uuid, source_identifiers uuid[]
) RETURNS TABLE (
    ledger_id uuid, payment_id uuid, return_id uuid, revision_id uuid, group_version integer,
    source_allocation_line_id uuid, source_return_no bigint, line_no integer,
    operation_id uuid, operation_version integer, journal_id uuid,
    payment_amount numeric, document_amount numeric, return_date date
) LANGUAGE sql STABLE STRICT SECURITY INVOKER SET search_path=pg_catalog AS $$
    WITH candidates AS (
        SELECT l.return_id FROM public.business_allocation_return_lines l
        WHERE l.ledger_id=ledger_identifier AND l.source_allocation_line_id=ANY(source_identifiers)
        UNION
        SELECT m.return_id FROM public.business_return_revision_members m
        WHERE m.ledger_id=ledger_identifier AND m.source_allocation_line_id=ANY(source_identifiers)
    ), latest AS (
        SELECT h.ledger_id, h.payment_id, h.id AS return_id, h.return_date AS original_date,
               r.id AS revision_id, r.group_version, r.action, r.return_date
        FROM candidates c JOIN public.business_allocation_returns h
            ON h.id=c.return_id AND h.ledger_id=ledger_identifier
        LEFT JOIN LATERAL (
            SELECT v.id, v.group_version, v.action, v.return_date
            FROM public.business_return_revisions v
            WHERE v.return_id=h.id AND v.payment_id=h.payment_id AND v.ledger_id=h.ledger_id
            ORDER BY v.group_version DESC LIMIT 1
        ) r ON true
    )
    SELECT l.ledger_id, l.payment_id, l.return_id, NULL::uuid, 1, l.source_allocation_line_id,
           l.source_return_no, l.line_no, l.operation_id, 1, l.journal_id,
           l.payment_amount, l.document_amount, h.original_date
    FROM latest h JOIN public.business_allocation_return_lines l
      ON (l.return_id,l.payment_id,l.ledger_id)=(h.return_id,h.payment_id,h.ledger_id)
    WHERE h.revision_id IS NULL AND l.source_allocation_line_id=ANY(source_identifiers)
    UNION ALL
    SELECT m.ledger_id, m.payment_id, m.return_id, h.revision_id, h.group_version,
           m.source_allocation_line_id, m.source_return_no, m.line_no, m.operation_id,
           m.operation_version, m.journal_id, m.payment_amount, m.document_amount, h.return_date
    FROM latest h JOIN public.business_return_revision_members m
      ON (m.revision_id,m.return_id,m.payment_id,m.ledger_id)
          =(h.revision_id,h.return_id,h.payment_id,h.ledger_id)
    WHERE h.action='correct' AND m.action IN ('create','correct')
      AND m.source_allocation_line_id=ANY(source_identifiers);
$$;
"""


def upgrade():
    op.execute("SELECT pg_advisory_xact_lock(18945999704708432)")
    op.execute(_CURRENT)


def downgrade():
    op.execute("SELECT pg_advisory_xact_lock(18945999704708432)")
    op.execute(
        "LOCK TABLE public.business_return_revisions, public.business_return_revision_members "
        "IN ACCESS EXCLUSIVE MODE"
    )
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM public.business_return_revisions)
                OR EXISTS (SELECT 1 FROM public.business_return_revision_members)
                OR EXISTS (SELECT 1 FROM public.change_log
                    WHERE entity_type IN
                        ('business_return_revisions','business_return_revision_members')
                        OR (entity_type='business_allocation_returns' AND entity_version>1)) THEN
                RAISE EXCEPTION USING ERRCODE='23514',
                    MESSAGE='Allocation return revision history prevents downgrade',
                    CONSTRAINT='ck_return_revision_downgrade';
            END IF;
        END; $$;
    """)
    op.execute("DROP FUNCTION coinpup_current_return_members(uuid,uuid[])")
