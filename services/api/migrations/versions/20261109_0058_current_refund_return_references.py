"""Resolve current explicit refund evidence for selected allocation returns."""

from alembic import op

revision = "20261109_0058"
down_revision = "20261109_0057"
branch_labels = None
depends_on = None


_REFERENCES = r"""
CREATE FUNCTION coinpup_current_refund_return_references(
    ledger_identifier uuid, payment_identifier uuid, return_identifiers uuid[]
)
RETURNS TABLE (
    return_id uuid, return_version integer, refund_id uuid, refund_version integer,
    revision_id uuid, line_no integer, refund_date date
) LANGUAGE sql STABLE STRICT SECURITY INVOKER SET search_path=pg_catalog AS $$
    WITH candidates AS (
        SELECT e.refund_id FROM public.business_refund_returns e
        WHERE e.ledger_id=ledger_identifier AND e.payment_id=payment_identifier
          AND e.return_id=ANY(return_identifiers)
        UNION
        SELECT e.refund_id FROM public.business_refund_revision_returns e
        WHERE e.ledger_id=ledger_identifier AND e.payment_id=payment_identifier
          AND e.return_id=ANY(return_identifiers)
    ), current_refunds AS (
        SELECT f.id AS refund_id, f.transaction_date AS original_date, v.id AS revision_id,
               v.operation_version, v.action, v.transaction_date
        FROM candidates c
        JOIN public.business_refunds f ON f.id=c.refund_id
            AND f.ledger_id=ledger_identifier AND f.payment_id=payment_identifier
        LEFT JOIN LATERAL (
            SELECT h.id, h.operation_version, h.action, h.transaction_date
            FROM public.business_refund_revisions h
            WHERE h.refund_id=f.id AND h.ledger_id=f.ledger_id AND h.payment_id=f.payment_id
            ORDER BY h.operation_version DESC LIMIT 1
        ) v ON true
    )
    SELECT e.return_id, 1, c.refund_id, 1, NULL::uuid, e.line_no, c.original_date
    FROM current_refunds c JOIN public.business_refund_returns e ON e.refund_id=c.refund_id
    WHERE c.revision_id IS NULL AND e.ledger_id=ledger_identifier
      AND e.payment_id=payment_identifier AND e.return_id=ANY(return_identifiers)
    UNION ALL
    SELECT e.return_id, e.return_version, c.refund_id, c.operation_version, c.revision_id,
           e.line_no, c.transaction_date
    FROM current_refunds c JOIN public.business_refund_revision_returns e
      ON e.revision_id=c.revision_id AND e.refund_id=c.refund_id
    WHERE c.action='correct' AND e.ledger_id=ledger_identifier
      AND e.payment_id=payment_identifier AND e.return_id=ANY(return_identifiers);
$$;
"""


def upgrade():
    op.execute("SELECT pg_advisory_xact_lock(18945999704708432)")
    op.execute(_REFERENCES)


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
    op.execute("DROP FUNCTION coinpup_current_refund_return_references(uuid,uuid,uuid[])")
