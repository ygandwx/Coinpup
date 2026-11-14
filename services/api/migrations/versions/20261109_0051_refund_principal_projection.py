"""Read the effective principal of each refund for a bounded set of ledger payments."""

from alembic import op

revision = "20261109_0051"
down_revision = "20261109_0050"
branch_labels = None
depends_on = None


_PRINCIPALS = r"""
CREATE FUNCTION coinpup_current_refund_principals(
    ledger_identifier uuid, payment_identifiers uuid[]
)
RETURNS TABLE (
    ledger_id uuid, payment_id uuid, refund_id uuid,
    operation_version integer, payment_amount numeric
) LANGUAGE sql STABLE STRICT SECURITY INVOKER SET search_path=pg_catalog AS $$
    SELECT f.ledger_id, f.payment_id, f.id, coalesce(latest.operation_version, 1),
           CASE WHEN latest.operation_version IS NULL THEN f.payment_amount
                ELSE latest.payment_amount END
    FROM public.business_refunds f
    LEFT JOIN LATERAL (
        SELECT v.operation_version, v.action, v.payment_amount
        FROM public.business_refund_revisions v
        WHERE v.refund_id=f.id AND v.payment_id=f.payment_id AND v.ledger_id=f.ledger_id
        ORDER BY v.operation_version DESC LIMIT 1
    ) latest ON true
    WHERE f.ledger_id=ledger_identifier AND f.payment_id=ANY(payment_identifiers)
        AND latest.action IS DISTINCT FROM 'cancel';
$$;
"""


def upgrade():
    op.execute("SELECT pg_advisory_xact_lock(18945999704708432)")
    op.execute(_PRINCIPALS)


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
    op.execute("DROP FUNCTION coinpup_current_refund_principals(uuid,uuid[])")
