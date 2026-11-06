"""Add bounded read projections; credit revision writes remain closed."""

from alembic import op

revision = "20261106_0042"
down_revision = "20261106_0041"
branch_labels = None
depends_on = None

_COLUMNS = """
    ledger_id uuid, credit_id uuid, document_id uuid, group_version integer, revision_id uuid,
    source_line_id uuid, source_operation_id uuid, line_no integer,
    operation_id uuid, operation_version integer, journal_id uuid,
    net_amount numeric, tax_amount numeric, total_amount numeric, recognition_date date,
    credit_date date, credit_reason text, posting_revision_reason text
"""

_FACTS = (
    "CREATE FUNCTION coinpup_credit_posting_facts(ledger_identifier uuid, "
    "credit_identifiers uuid[], journal_identifiers uuid[] DEFAULT NULL) RETURNS TABLE ("
    + _COLUMNS
    + r""") LANGUAGE sql STABLE SECURITY INVOKER SET search_path=pg_catalog AS $$
    SELECT c.ledger_id, c.id, c.document_id, 1, NULL::uuid,
        l.source_line_id, l.source_operation_id, l.line_no,
        l.operation_id, 1, l.journal_id, l.net_amount, l.tax_amount, l.total_amount,
        l.recognition_date, c.credit_date, c.reason::text, NULL::text
    FROM public.business_credits c JOIN public.business_credit_lines l
        ON l.credit_id=c.id AND l.document_id=c.document_id AND l.ledger_id=c.ledger_id
    WHERE c.ledger_id=ledger_identifier
        AND (credit_identifiers IS NOT NULL OR journal_identifiers IS NOT NULL)
        AND (credit_identifiers IS NULL OR c.id=ANY(credit_identifiers))
        AND (journal_identifiers IS NULL OR l.journal_id=ANY(journal_identifiers))
    UNION ALL
    SELECT r.ledger_id, r.credit_id, r.document_id, r.group_version, r.id,
        m.source_line_id, m.source_operation_id, m.line_no,
        m.operation_id, m.operation_version, m.journal_id,
        m.net_amount, m.tax_amount, m.total_amount, m.recognition_date,
        r.credit_date, r.credit_reason::text,
        CASE WHEN m.action='correct' THEN r.revision_reason::text ELSE NULL::text END
    FROM public.business_credit_revisions r JOIN public.business_credit_revision_members m
        ON m.revision_id=r.id AND m.credit_id=r.credit_id AND m.document_id=r.document_id
            AND m.ledger_id=r.ledger_id
    WHERE r.ledger_id=ledger_identifier AND m.action IN ('create','correct')
        AND (credit_identifiers IS NOT NULL OR journal_identifiers IS NOT NULL)
        AND (credit_identifiers IS NULL OR r.credit_id=ANY(credit_identifiers))
        AND (journal_identifiers IS NULL OR m.journal_id=ANY(journal_identifiers));
$$;
"""
)

_CURRENT = (
    "CREATE FUNCTION coinpup_current_credit_lines(ledger_identifier uuid, "
    "source_identifiers uuid[]) RETURNS TABLE ("
    + _COLUMNS
    + r""") LANGUAGE sql STABLE SECURITY INVOKER SET search_path=pg_catalog AS $$
    WITH candidates AS (
        SELECT l.credit_id FROM public.business_credit_lines l
        WHERE l.ledger_id=ledger_identifier AND l.source_line_id=ANY(source_identifiers)
        UNION
        SELECT m.credit_id FROM public.business_credit_revision_members m
        WHERE m.ledger_id=ledger_identifier AND m.source_line_id=ANY(source_identifiers)
    ), latest AS (
        SELECT c.id, coalesce(r.group_version,1) AS group_version, r.action
        FROM public.business_credits c JOIN candidates x ON x.credit_id=c.id
        LEFT JOIN LATERAL (
            SELECT v.group_version, v.action FROM public.business_credit_revisions v
            WHERE v.credit_id=c.id AND v.ledger_id=c.ledger_id
            ORDER BY v.group_version DESC LIMIT 1
        ) r ON true
        WHERE c.ledger_id=ledger_identifier
    )
    SELECT f.* FROM public.coinpup_credit_posting_facts(
        ledger_identifier, ARRAY(SELECT id FROM latest), NULL) f
    JOIN latest x ON x.id=f.credit_id AND x.group_version=f.group_version
    WHERE x.action IS DISTINCT FROM 'cancel' AND f.source_line_id=ANY(source_identifiers);
$$;
"""
)


def upgrade():
    op.execute("SELECT pg_advisory_xact_lock(18945999704708432)")
    op.execute(_FACTS)
    op.execute(_CURRENT)


def downgrade():
    op.execute("SELECT pg_advisory_xact_lock(18945999704708432)")
    op.execute(
        "LOCK TABLE public.business_credit_revisions, "
        "public.business_credit_revision_members IN ACCESS EXCLUSIVE MODE"
    )
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM public.business_credit_revisions)
               OR EXISTS (SELECT 1 FROM public.business_credit_revision_members)
               OR EXISTS (SELECT 1 FROM public.change_log WHERE entity_type IN
                   ('business_credit_revision_members','business_credit_revisions')
                   OR (entity_type='business_credits' AND entity_version>1)) THEN
                RAISE EXCEPTION USING ERRCODE='23514',
                    MESSAGE='Credit revision history prevents downgrade',
                    CONSTRAINT='ck_credit_revision_downgrade';
            END IF;
        END; $$;
    """)
    op.execute("DROP FUNCTION coinpup_current_credit_lines(uuid,uuid[])")
    op.execute("DROP FUNCTION coinpup_credit_posting_facts(uuid,uuid[],uuid[])")
