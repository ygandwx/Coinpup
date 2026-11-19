"""Prove a complete document version in a caller-owned write transaction."""

from alembic import op

revision = "20261113_0095"
down_revision = "20261113_0094"
branch_labels = None
depends_on = None

_PROOF = """
CREATE FUNCTION coinpup_validate_document_revision(ledger_identifier uuid,
    document_identifier uuid,revision_identifier uuid) RETURNS void
LANGUAGE plpgsql SET search_path=pg_catalog AS $$
DECLARE h public.business_document_revisions%ROWTYPE; operation_identifier uuid;
BEGIN
    PERFORM pg_advisory_xact_lock(18945999704708432);
    IF current_setting('transaction_isolation') <> 'read committed' THEN
        RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='Invalid document revision isolation',
            CONSTRAINT='ck_document_revision_isolation';
    END IF;
    SELECT * INTO h FROM public.business_document_revisions
    WHERE (id,document_id,ledger_id)=(revision_identifier,document_identifier,ledger_identifier);
    IF NOT FOUND THEN
        RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='Document revision scope is invalid',
            CONSTRAINT='ck_document_revision_scope';
    END IF;
    -- This proves complete previous/replacement membership and its exact permanent 200.
    PERFORM public.coinpup_validate_document_revision_receipt(h.ledger_id,h.idempotency_key);
    IF EXISTS (
        SELECT 1 FROM public.business_document_accruals a
        LEFT JOIN public.journals r ON r.id=a.reversal_journal_id
        WHERE (a.revision_id,a.document_id,a.ledger_id)=(h.id,h.document_id,h.ledger_id)
            AND a.reversal_journal_id IS NOT NULL
            AND (r.id IS NULL OR r.revision_reason IS DISTINCT FROM h.reason)
    ) THEN
        RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='Invalid accrual reversal reason',
            CONSTRAINT='ck_document_accrual_journals';
    END IF;
    FOR operation_identifier IN SELECT DISTINCT a.operation_id
        FROM public.business_document_accruals a
        WHERE (a.revision_id,a.document_id,a.ledger_id)=(h.id,h.document_id,h.ledger_id)
        ORDER BY a.operation_id LOOP
        -- Existing proofs check every posting, reversal and continuous head, and seal them.
        PERFORM public.coinpup_validate_operation_history(operation_identifier);
        PERFORM public.coinpup_validate_document_operation_head(operation_identifier);
    END LOOP;
    PERFORM public.coinpup_validate_business_confirmation(h.confirmation_id);
    PERFORM public.coinpup_validate_document_dependencies(h.ledger_id,h.document_id);
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
                RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='Document group history exists',
                    CONSTRAINT='ck_document_group_downgrade';
            END IF;
        END; $$;
    """)
    op.execute("DROP FUNCTION coinpup_validate_document_revision(uuid,uuid,uuid)")
