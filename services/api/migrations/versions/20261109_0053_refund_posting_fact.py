"""Resolve the immutable facts of a single refund posting within its ledger."""

from alembic import op

revision = "20261109_0053"
down_revision = "20261109_0052"
branch_labels = None
depends_on = None


_POSTING_FACT = r"""
CREATE FUNCTION coinpup_refund_posting_fact(ledger_identifier uuid, journal_identifier uuid)
RETURNS TABLE (
    ledger_id uuid, refund_id uuid, payment_id uuid, operation_id uuid, journal_id uuid,
    operation_version integer, revision_id uuid, revision_reason text, created_by uuid,
    account_id uuid, asset_id text, amount numeric, payment_asset_id text, payment_amount numeric,
    source_version integer, transaction_date date, recognition_date date, reason text, fees jsonb,
    allocation_return_ids uuid[]
) LANGUAGE plpgsql STABLE STRICT SECURITY INVOKER SET search_path=pg_catalog AS $$
DECLARE matches integer;
BEGIN
    RETURN QUERY
    SELECT f.ledger_id, f.id, f.payment_id, f.operation_id, f.journal_id, 1,
        NULL::uuid, NULL::text, f.created_by, f.account_id, f.asset_id::text,
        f.amount, p.asset_id::text, f.payment_amount, f.source_version,
        f.transaction_date, f.transaction_date, f.reason::text, f.fees,
        ARRAY(SELECT e.return_id FROM public.business_refund_returns e
              WHERE e.ledger_id=f.ledger_id AND e.refund_id=f.id AND e.payment_id=f.payment_id
              ORDER BY e.line_no)
    FROM public.business_refunds f
    JOIN public.business_payments p ON p.id=f.payment_id AND p.ledger_id=f.ledger_id
    WHERE f.ledger_id=ledger_identifier AND f.journal_id=journal_identifier
    UNION ALL
    SELECT r.ledger_id, r.refund_id, r.payment_id, r.operation_id, r.replacement_journal_id,
        r.operation_version, r.id, r.revision_reason::text, r.created_by, r.account_id,
        r.asset_id::text, r.amount, p.asset_id::text, r.payment_amount, f.source_version,
        r.transaction_date, r.transaction_date, r.reason::text, r.fees,
        ARRAY(SELECT e.return_id FROM public.business_refund_revision_returns e
              WHERE e.ledger_id=r.ledger_id AND e.refund_id=r.refund_id
                AND e.payment_id=r.payment_id AND e.revision_id=r.id ORDER BY e.line_no)
    FROM public.business_refund_revisions r
    JOIN public.business_refunds f ON f.id=r.refund_id AND f.payment_id=r.payment_id
        AND f.operation_id=r.operation_id AND f.ledger_id=r.ledger_id
    JOIN public.business_payments p ON p.id=f.payment_id AND p.ledger_id=f.ledger_id
    WHERE r.ledger_id=ledger_identifier AND r.replacement_journal_id=journal_identifier
        AND r.action='correct';
    GET DIAGNOSTICS matches = ROW_COUNT;
    IF matches>1 THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='Refund posting identity is ambiguous',
            CONSTRAINT='ck_refund_posting_fact';
    END IF;
END; $$;
"""


def upgrade():
    op.execute("SELECT pg_advisory_xact_lock(18945999704708432)")
    op.execute(_POSTING_FACT)


def downgrade():
    op.execute("SELECT pg_advisory_xact_lock(18945999704708432)")
    op.execute(
        "LOCK TABLE public.business_refund_revisions, public.business_refund_revision_returns "
        "IN ACCESS EXCLUSIVE MODE"
    )
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM public.business_refund_revisions)
                OR EXISTS (SELECT 1 FROM public.business_refund_revision_returns)
                OR EXISTS (SELECT 1 FROM public.change_log WHERE
                    entity_type IN ('business_refund_revisions','business_refund_revision_returns')
                    OR (entity_type='business_refunds' AND entity_version>1)) THEN
                RAISE EXCEPTION USING ERRCODE='23514',
                    MESSAGE='Refund revision history prevents downgrade',
                    CONSTRAINT='ck_refund_revision_downgrade';
            END IF;
        END; $$;
    """)
    op.execute("DROP FUNCTION coinpup_refund_posting_fact(uuid,uuid)")
