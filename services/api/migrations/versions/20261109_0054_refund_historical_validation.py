"""Verify immutable refund creation receipts and historical posting legs."""

from alembic import op

revision = "20261109_0054"
down_revision = "20261109_0053"
branch_labels = None
depends_on = None


_POSTING = r"""
CREATE FUNCTION coinpup_validate_refund_posting_fact(journal_identifier uuid, common_result jsonb)
RETURNS void LANGUAGE plpgsql SECURITY INVOKER SET search_path=pg_catalog AS $$
DECLARE fact record; entry public.business_refunds%ROWTYPE;
        payment public.business_payments%ROWTYPE;
        header public.journals%ROWTYPE; operation public.financial_operations%ROWTYPE;
        original public.financial_operations%ROWTYPE; actual_common jsonb; last_position integer;
        cash_sign integer; control_key text; same_asset boolean;
        cash_scale integer; principal_scale integer; fee jsonb; fee_component integer;
        precision_digits integer; fee_amount numeric;
BEGIN
    SELECT * INTO header FROM public.journals WHERE id=journal_identifier;
    SELECT * INTO fact FROM public.coinpup_refund_posting_fact(
        header.ledger_id, journal_identifier);
    IF NOT FOUND THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='Business refund shape is invalid',
            CONSTRAINT='ck_business_refund_shape';
    END IF;
    SELECT * INTO entry FROM public.business_refunds WHERE id=fact.refund_id;
    SELECT * INTO payment FROM public.business_payments WHERE id=fact.payment_id;
    SELECT * INTO operation FROM public.financial_operations WHERE id=fact.operation_id;
    SELECT * INTO original FROM public.financial_operations WHERE id=payment.operation_id;
    SELECT scale INTO cash_scale FROM public.assets WHERE asset_id=fact.asset_id;
    SELECT scale INTO principal_scale FROM public.assets WHERE asset_id=payment.asset_id;
    IF fact.revision_id IS NULL THEN
        SELECT max(line_no) INTO last_position FROM public.business_refund_returns
            WHERE refund_id=fact.refund_id;
    ELSE
        SELECT max(line_no) INTO last_position FROM public.business_refund_revision_returns
            WHERE revision_id=fact.revision_id;
    END IF;
    cash_sign := CASE payment.direction WHEN 'receipt' THEN -1 ELSE 1 END;
    control_key := CASE payment.direction WHEN 'receipt' THEN 'advance.received'
        ELSE 'advance.paid' END;
    same_asset := fact.asset_id=payment.asset_id;
    IF entry.ledger_id IS DISTINCT FROM fact.ledger_id
       OR payment.ledger_id IS DISTINCT FROM fact.ledger_id
       OR payment.direction NOT IN ('receipt','payout')
       OR original.kind IS DISTINCT FROM 'payment'
       OR original.ledger_id IS DISTINCT FROM fact.ledger_id
       OR original.version IS DISTINCT FROM 1 OR original.status IS DISTINCT FROM 'active'
       OR original.current_journal_id IS DISTINCT FROM payment.journal_id
       OR fact.source_version IS DISTINCT FROM original.version
       OR operation.kind IS DISTINCT FROM 'refund'
       OR operation.ledger_id IS DISTINCT FROM fact.ledger_id
       OR operation.created_by IS DISTINCT FROM entry.created_by
       OR header.operation_id IS DISTINCT FROM fact.operation_id
       OR header.ledger_id IS DISTINCT FROM fact.ledger_id
       OR header.operation_version IS DISTINCT FROM fact.operation_version
       OR header.journal_kind IS DISTINCT FROM 'posting' OR header.reverses_journal_id IS NOT NULL
       OR header.transaction_date IS DISTINCT FROM fact.transaction_date
       OR header.recognition_date IS DISTINCT FROM fact.recognition_date
       OR header.description IS DISTINCT FROM fact.reason
       OR header.revision_reason IS DISTINCT FROM fact.revision_reason
       OR fact.transaction_date < payment.transaction_date
       OR cash_scale IS NULL OR principal_scale IS NULL
       OR trunc(fact.amount,cash_scale) <> fact.amount
       OR trunc(fact.payment_amount,principal_scale) <> fact.payment_amount
       OR (same_asset AND fact.amount<>fact.payment_amount)
       OR coalesce(last_position,0) IS DISTINCT FROM cardinality(fact.allocation_return_ids)
       OR cardinality(fact.allocation_return_ids)>200
       OR EXISTS (SELECT 1 FROM unnest(fact.allocation_return_ids) AS x(return_id)
           LEFT JOIN public.business_allocation_returns r ON r.id=x.return_id
              AND r.payment_id=fact.payment_id AND r.ledger_id=fact.ledger_id
           WHERE r.id IS NULL OR r.return_date>fact.transaction_date)
       OR EXISTS (SELECT 1 FROM public.opening_positions WHERE operation_id=operation.id)
       OR (SELECT count(*) FROM public.journal_lines WHERE journal_id=header.id)
           <> (CASE WHEN same_asset THEN 2 ELSE 4 END)+2*jsonb_array_length(fact.fees)
       OR (SELECT count(*) FROM public.journal_lines WHERE journal_id=header.id AND component_no=0)
           <> (CASE WHEN same_asset THEN 2 ELSE 4 END)
       OR EXISTS (SELECT 1 FROM public.journal_lines l WHERE l.journal_id=header.id
           AND l.component_no=0 AND (l.party_id IS DISTINCT FROM payment.party_id
               OR l.document_id IS NOT NULL OR l.document_line_id IS NOT NULL
               OR l.project_id IS NOT NULL OR l.counterparty_entity_id IS NOT NULL
               OR l.dimension_owner_id IS NOT NULL))
       OR (SELECT count(*) FROM public.journal_lines l JOIN public.accounts a ON a.id=l.account_id
           WHERE l.journal_id=header.id AND l.component_no=0 AND l.role='account'
               AND l.account_id=fact.account_id AND a.account_class='money'
               AND l.asset_id=fact.asset_id AND l.amount=cash_sign*fact.amount) <> 1
       OR (SELECT count(*) FROM public.journal_lines l JOIN public.accounts a ON a.id=l.account_id
           WHERE l.journal_id=header.id AND l.component_no=0 AND l.role='account'
               AND a.system_key=control_key AND l.asset_id=payment.asset_id
               AND l.amount=-cash_sign*fact.payment_amount) <> 1
       OR (NOT same_asset AND (
           (SELECT count(*) FROM public.journal_lines l WHERE l.journal_id=header.id
               AND l.component_no=0 AND l.role='exchange' AND l.asset_id=fact.asset_id
               AND l.amount=-cash_sign*fact.amount) <> 1
           OR (SELECT count(*) FROM public.journal_lines l WHERE l.journal_id=header.id
               AND l.component_no=0 AND l.role='exchange' AND l.asset_id=payment.asset_id
               AND l.amount=cash_sign*fact.payment_amount) <> 1)) THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='Business refund shape is invalid',
            CONSTRAINT='ck_business_refund_shape';
    END IF;
    -- Independent fees use the same strict two-leg component shape as payments.
    FOR fee, fee_component IN SELECT value, ordinality FROM jsonb_array_elements(fact.fees)
        WITH ORDINALITY LOOP
        IF jsonb_typeof(fee) IS DISTINCT FROM 'object' THEN
            RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='Refund fee shape is invalid',
                CONSTRAINT='ck_business_refund_shape';
        END IF;
        IF fee-ARRAY['account_id','asset_id','amount','category_id'] <> '{}'::jsonb
           OR jsonb_typeof(fee->'account_id') IS DISTINCT FROM 'string'
           OR jsonb_typeof(fee->'asset_id') IS DISTINCT FROM 'string'
           OR jsonb_typeof(fee->'amount') IS DISTINCT FROM 'string'
           OR jsonb_typeof(fee->'category_id') IS DISTINCT FROM 'string' THEN
            RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='Refund fee shape is invalid',
                CONSTRAINT='ck_business_refund_shape';
        END IF;
        SELECT scale INTO precision_digits FROM public.assets WHERE asset_id=fee->>'asset_id';
        IF precision_digits IS NULL OR fee->>'amount' !~ '^(0|[1-9][0-9]{0,19})(\.[0-9]{1,18})?$'
           OR length(split_part(fee->>'amount','.',2)) > precision_digits THEN
            RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='Refund fee quantity is invalid',
                CONSTRAINT='ck_business_refund_shape';
        END IF;
        fee_amount := (fee->>'amount')::numeric;
        IF fee_amount<=0 OR fee_amount>=100000000000000000000
           OR (SELECT count(*) FROM public.journal_lines
               WHERE journal_id=header.id AND component_no=fee_component) <> 2
           OR EXISTS (SELECT 1 FROM public.journal_lines l WHERE l.journal_id=header.id
               AND l.component_no=fee_component AND (l.asset_id IS DISTINCT FROM fee->>'asset_id'
                   OR l.party_id IS NOT NULL OR l.document_id IS NOT NULL
                   OR l.document_line_id IS NOT NULL OR l.project_id IS NOT NULL
                   OR l.counterparty_entity_id IS NOT NULL OR l.dimension_owner_id IS NOT NULL))
           OR (SELECT count(*) FROM public.journal_lines l
               JOIN public.accounts a ON a.id=l.account_id
               WHERE l.journal_id=header.id AND l.component_no=fee_component AND l.role='account'
                   AND a.account_class='money' AND l.account_id::text=fee->>'account_id'
                   AND l.amount=-fee_amount) <> 1
           OR (SELECT count(*) FROM public.journal_lines l
               WHERE l.journal_id=header.id AND l.component_no=fee_component AND l.role='expense'
                   AND l.category_id::text=fee->>'category_id' AND l.amount=fee_amount) <> 1 THEN
            RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='Refund fee shape is invalid',
                CONSTRAINT='ck_business_refund_shape';
        END IF;
    END LOOP;
    actual_common := public.coinpup_validate_journal_common(journal_identifier);
    IF actual_common->>'error_message' IS NOT NULL THEN
        RAISE EXCEPTION USING ERRCODE=actual_common->>'error_sqlstate',
            MESSAGE=actual_common->>'error_message', CONSTRAINT=actual_common->>'error_constraint';
    END IF;
    IF common_result IS DISTINCT FROM actual_common THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='Business refund shape is invalid',
            CONSTRAINT='ck_business_refund_shape';
    END IF;
END; $$;
"""

_ORIGIN = r"""
CREATE FUNCTION coinpup_validate_refund_origin_facts(refund_identifier uuid) RETURNS void
LANGUAGE plpgsql SECURITY INVOKER SET search_path=pg_catalog AS $$
DECLARE entry public.business_refunds%ROWTYPE; receipt public.command_receipts%ROWTYPE;
BEGIN
    SELECT * INTO entry FROM public.business_refunds WHERE id=refund_identifier;
    SELECT * INTO receipt FROM public.command_receipts
        WHERE ledger_id=entry.ledger_id AND key=entry.idempotency_key;
    IF entry.id IS NULL OR current_setting('transaction_isolation') <> 'read committed'
       OR entry.idempotency_key=(SELECT idempotency_key FROM public.business_payments
           WHERE id=entry.payment_id AND ledger_id=entry.ledger_id)
       OR receipt.hash_version IS DISTINCT FROM 2 OR receipt.response_status IS DISTINCT FROM 201
       OR receipt.operation_id IS DISTINCT FROM entry.operation_id
       OR receipt.response::text IS DISTINCT FROM
          public.coinpup_business_refund_response(entry.id)::text THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='Business refund shape is invalid',
            CONSTRAINT='ck_business_refund_shape';
    END IF;
    PERFORM public.coinpup_validate_refund_posting_fact(entry.journal_id,
        public.coinpup_validate_journal_common(entry.journal_id));
END; $$;
"""


def upgrade():
    op.execute("SELECT pg_advisory_xact_lock(18945999704708432)")
    op.execute(_POSTING)
    op.execute(_ORIGIN)


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
    op.execute("DROP FUNCTION coinpup_validate_refund_origin_facts(uuid)")
    op.execute("DROP FUNCTION coinpup_validate_refund_posting_fact(uuid,jsonb)")
