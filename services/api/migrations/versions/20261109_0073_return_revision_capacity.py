"""Check the effective payment and all affected document lines before a group may change."""

from alembic import op

revision = "20261109_0073"
down_revision = "20261109_0072"
branch_labels = None
depends_on = None


_CAPACITY = r"""
CREATE FUNCTION coinpup_validate_return_capacity(ledger_identifier uuid, payment_identifier uuid,
    source_identifiers uuid[]) RETURNS void
LANGUAGE plpgsql STABLE SECURITY INVOKER SET search_path=pg_catalog AS $$
DECLARE principal numeric; applied numeric; refunded numeric; documents uuid[];
BEGIN
    SELECT p.amount INTO principal FROM public.business_payments p
    WHERE (p.id,p.ledger_id)=(payment_identifier,ledger_identifier);
    IF principal IS NULL OR coalesce(cardinality(source_identifiers),0) NOT BETWEEN 1 AND 400
       OR EXISTS (SELECT 1 FROM unnest(source_identifiers) requested(id)
            WHERE NOT EXISTS (SELECT 1 FROM public.business_allocation_lines a
               WHERE (a.id,a.payment_id,a.ledger_id)=
                  (requested.id,payment_identifier,ledger_identifier))) THEN
        RAISE EXCEPTION USING ERRCODE='23514',
            MESSAGE='Allocation return capacity scope is invalid',
            CONSTRAINT='ck_return_revision_capacity';
    END IF;
    SELECT coalesce(sum(a.payment_amount),0) INTO applied
    FROM public.business_allocation_lines a
    WHERE (a.payment_id,a.ledger_id)=(payment_identifier,ledger_identifier);
    applied := applied-public.coinpup_returned_payment_amount(ledger_identifier,payment_identifier);
    SELECT coalesce(sum(f.payment_amount),0) INTO refunded
    FROM public.coinpup_current_refund_principals(ledger_identifier,ARRAY[payment_identifier]) f;
    IF applied<0 OR principal-applied-refunded<0 THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='Allocation return exceeds payment capacity',
            CONSTRAINT='ck_return_revision_payment_capacity';
    END IF;
    SELECT array_agg(DISTINCT a.document_line_id) INTO documents
    FROM public.business_allocation_lines a WHERE a.ledger_id=ledger_identifier
      AND a.payment_id=payment_identifier AND a.id=ANY(source_identifiers);
    -- Include every payment allocating to these document lines, in their own asset.
    IF EXISTS (
        WITH allocations AS MATERIALIZED (
            SELECT a.id,a.document_line_id,a.document_amount FROM public.business_allocation_lines a
            WHERE a.ledger_id=ledger_identifier AND a.document_line_id=ANY(documents)
        ), charges AS (
            SELECT document_line_id,sum(document_amount) AS quantity FROM allocations
            GROUP BY document_line_id
        ), returns AS (
            SELECT a.document_line_id,sum(r.document_amount) AS quantity
            FROM public.coinpup_current_return_members(ledger_identifier,
                ARRAY(SELECT id FROM allocations)) r
            JOIN allocations a ON a.id=r.source_allocation_line_id GROUP BY a.document_line_id
        ), credits AS (
            SELECT c.source_line_id,sum(c.net_amount) AS net,sum(c.tax_amount) AS tax,
                sum(c.total_amount) AS gross
            FROM public.coinpup_current_credit_lines(ledger_identifier,documents) c
            GROUP BY c.source_line_id
        )
        SELECT 1 FROM charges a JOIN public.business_document_lines d
            ON (d.id,d.ledger_id)=(a.document_line_id,ledger_identifier)
        LEFT JOIN returns r ON r.document_line_id=d.id
        LEFT JOIN credits c ON c.source_line_id=d.id
        WHERE a.quantity-coalesce(r.quantity,0)<0
           OR a.quantity-coalesce(r.quantity,0)+coalesce(c.gross,0)>d.total_amount
           OR coalesce(c.net,0)>d.net_amount OR coalesce(c.tax,0)>d.tax_amount
    ) THEN
        RAISE EXCEPTION USING ERRCODE='23514',
            MESSAGE='Allocation return exceeds document capacity',
            CONSTRAINT='ck_return_revision_document_capacity';
    END IF;
END; $$;
"""


def upgrade():
    op.execute("SELECT pg_advisory_xact_lock(18945999704708432)")
    op.execute(_CAPACITY)


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
    op.execute("DROP FUNCTION coinpup_validate_return_capacity(uuid,uuid,uuid[])")
