"""Prove fixed refund revision receipts against immutable neighboring versions."""

from alembic import op

revision = "20261109_0055"
down_revision = "20261109_0054"
branch_labels = None
depends_on = None


_RESPONSE = r"""
CREATE FUNCTION coinpup_business_refund_revision_response(revision_identifier uuid) RETURNS jsonb
LANGUAGE plpgsql STABLE SECURITY INVOKER SET search_path=pg_catalog AS $$
DECLARE entry public.business_refund_revisions%ROWTYPE; item record; payload jsonb;
    previous_fact jsonb; replacement_fact jsonb;
BEGIN
    SELECT * INTO entry FROM public.business_refund_revisions WHERE id=revision_identifier;
    IF NOT FOUND THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='Refund revision response is inconsistent',
            CONSTRAINT='ck_refund_revision_shape';
    END IF;
    FOR item IN
        SELECT source.previous, source.expected_revision, source.expected_version, f.*
        FROM (VALUES
            (true, entry.previous_journal_id, entry.previous_revision_id,
             entry.operation_version-1),
            (false, entry.replacement_journal_id, entry.id, entry.operation_version)
        ) AS source(previous, journal_id, expected_revision, expected_version)
        CROSS JOIN LATERAL public.coinpup_refund_posting_fact(entry.ledger_id, source.journal_id) f
    LOOP
        IF ROW(item.refund_id,item.payment_id,item.operation_id,item.revision_id,
               item.operation_version) IS DISTINCT FROM ROW(entry.refund_id,entry.payment_id,
               entry.operation_id,item.expected_revision,item.expected_version) THEN
            RAISE EXCEPTION USING ERRCODE='23514',
                MESSAGE='Refund revision response is inconsistent',
                CONSTRAINT='ck_refund_revision_shape';
        END IF;
        payload := jsonb_build_object(
            'account_id',item.account_id,'asset_id',item.asset_id,'amount',item.amount::text,
            'payment_asset_id',item.payment_asset_id,'payment_amount',item.payment_amount::text,
            'source_version',item.source_version,'transaction_date',item.transaction_date,
            'recognition_date',item.recognition_date,'reason',item.reason,'fees',item.fees,
            'allocation_return_ids',item.allocation_return_ids);
        IF item.previous THEN previous_fact := payload; ELSE replacement_fact := payload; END IF;
    END LOOP;
    IF previous_fact IS NULL OR (entry.action='correct')<>(replacement_fact IS NOT NULL) THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='Refund revision response is inconsistent',
            CONSTRAINT='ck_refund_revision_shape';
    END IF;
    RETURN jsonb_build_object(
        'kind','business_refund_revision','refund_id',entry.refund_id,'revision_id',entry.id,
        'ledger_id',entry.ledger_id,'payment_id',entry.payment_id,'operation_id',entry.operation_id,
        'previous_version',entry.operation_version-1,'version',entry.operation_version,
        'action',entry.action,'reason',entry.revision_reason,
        'previous_journal_id',entry.previous_journal_id,
        'reversal_journal_id',entry.reversal_journal_id,
        'replacement_journal_id',entry.replacement_journal_id,
        'previous',previous_fact,'replacement',replacement_fact);
END; $$;
"""

_RECEIPT = r"""
CREATE FUNCTION coinpup_validate_refund_revision_receipt(revision_identifier uuid) RETURNS void
LANGUAGE plpgsql SECURITY INVOKER SET search_path=pg_catalog AS $$
DECLARE entry public.business_refund_revisions%ROWTYPE; receipt public.command_receipts%ROWTYPE;
BEGIN
    SELECT * INTO entry FROM public.business_refund_revisions WHERE id=revision_identifier;
    SELECT * INTO receipt FROM public.command_receipts
        WHERE ledger_id=entry.ledger_id AND key=entry.idempotency_key;
    IF entry.id IS NULL OR receipt.operation_id IS DISTINCT FROM entry.operation_id
       OR receipt.hash_version IS DISTINCT FROM 2 OR receipt.response_status IS DISTINCT FROM 200
       OR receipt.response::text IS DISTINCT FROM
            public.coinpup_business_refund_revision_response(entry.id)::text THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='Refund revision receipt is inconsistent',
            CONSTRAINT='ck_refund_revision_shape';
    END IF;
END; $$;
"""


def upgrade():
    op.execute("SELECT pg_advisory_xact_lock(18945999704708432)")
    op.execute(_RESPONSE)
    op.execute(_RECEIPT)


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
    op.execute("DROP FUNCTION coinpup_validate_refund_revision_receipt(uuid)")
    op.execute("DROP FUNCTION coinpup_business_refund_revision_response(uuid)")
