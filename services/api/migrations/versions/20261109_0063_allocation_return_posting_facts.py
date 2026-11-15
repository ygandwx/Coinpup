"""Resolve historical allocation return journals without consulting today's effective balance."""

from alembic import op

revision = "20261109_0063"
down_revision = "20261109_0062"
branch_labels = None
depends_on = None


_FACTS = r"""
CREATE FUNCTION coinpup_return_posting_facts(
    ledger_identifier uuid, return_identifiers uuid[], journal_identifiers uuid[]
) RETURNS TABLE (
    ledger_id uuid, return_id uuid, payment_id uuid, revision_id uuid, group_version integer,
    source_allocation_line_id uuid, source_operation_id uuid, source_version integer,
    source_return_no bigint, line_no integer, operation_id uuid, operation_version integer,
    journal_id uuid, payment_amount numeric, document_amount numeric,
    return_date date, return_reason text, revision_reason text, created_by uuid,
    party_id uuid, direction text, payment_asset_id text, document_id uuid, document_line_id uuid,
    document_asset_id text, project_id uuid
) LANGUAGE sql STABLE SECURITY INVOKER SET search_path=pg_catalog AS $$
    WITH facts AS (
        SELECT l.ledger_id, l.return_id, l.payment_id, NULL::uuid AS revision_id,
               1 AS group_version, l.source_allocation_line_id, l.source_version,
               l.source_return_no, l.line_no, l.operation_id, 1 AS operation_version,
               l.journal_id, l.payment_amount, l.document_amount, g.return_date,
               g.reason::text AS return_reason, NULL::text AS revision_reason, g.created_by
        FROM public.business_allocation_return_lines l
        JOIN public.business_allocation_returns g
          ON (g.id,g.payment_id,g.ledger_id)=(l.return_id,l.payment_id,l.ledger_id)
        WHERE l.ledger_id=ledger_identifier
          AND (return_identifiers IS NOT NULL OR journal_identifiers IS NOT NULL)
          AND (return_identifiers IS NULL OR l.return_id=ANY(return_identifiers))
          AND (journal_identifiers IS NULL OR l.journal_id=ANY(journal_identifiers))
        UNION ALL
        SELECT m.ledger_id, m.return_id, m.payment_id, v.id, v.group_version,
               m.source_allocation_line_id, m.source_version, m.source_return_no, m.line_no,
               m.operation_id, m.operation_version, m.journal_id, m.payment_amount,
               m.document_amount, v.return_date, v.return_reason::text, v.revision_reason::text,
               v.created_by
        FROM public.business_return_revision_members m
        JOIN public.business_return_revisions v
          ON (v.id,v.return_id,v.payment_id,v.ledger_id)
              =(m.revision_id,m.return_id,m.payment_id,m.ledger_id)
        WHERE m.ledger_id=ledger_identifier AND v.action='correct'
          AND m.action IN ('create','correct')
          AND (return_identifiers IS NOT NULL OR journal_identifiers IS NOT NULL)
          AND (return_identifiers IS NULL OR m.return_id=ANY(return_identifiers))
          AND (journal_identifiers IS NULL OR m.journal_id=ANY(journal_identifiers))
    )
    SELECT f.ledger_id, f.return_id, f.payment_id, f.revision_id, f.group_version,
           f.source_allocation_line_id, a.operation_id, f.source_version, f.source_return_no,
           f.line_no, f.operation_id, f.operation_version, f.journal_id,
           f.payment_amount, f.document_amount, f.return_date, f.return_reason,
           f.revision_reason, f.created_by, p.party_id, p.direction::text, p.asset_id::text,
           a.document_id, a.document_line_id, d.asset_id::text, l.project_id
    FROM facts f JOIN public.business_allocation_lines a
      ON (a.id,a.payment_id,a.ledger_id)=(f.source_allocation_line_id,f.payment_id,f.ledger_id)
    JOIN public.business_payments p ON (p.id,p.ledger_id)=(f.payment_id,f.ledger_id)
    JOIN public.business_documents d ON (d.id,d.ledger_id)=(a.document_id,a.ledger_id)
    JOIN public.business_document_lines l
      ON (l.id,l.document_id,l.ledger_id)=(a.document_line_id,a.document_id,a.ledger_id);
$$;
"""


def upgrade():
    op.execute("SELECT pg_advisory_xact_lock(18945999704708432)")
    op.execute(_FACTS)


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
    op.execute("DROP FUNCTION coinpup_return_posting_facts(uuid,uuid[],uuid[])")
