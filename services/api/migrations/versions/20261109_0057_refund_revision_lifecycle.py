"""Validate and seal complete refund correction and cancellation chains."""

from alembic import op

revision = "20261109_0057"
down_revision = "20261109_0056"
branch_labels = None
depends_on = None


_OLD_REFUND = (
    r"""
CREATE OR REPLACE FUNCTION coinpup_validate_business_refund(refund_identifier uuid)
RETURNS void LANGUAGE plpgsql SET search_path=pg_catalog AS $$
DECLARE entry public.business_refunds%ROWTYPE; payment public.business_payments%ROWTYPE;
        header public.journals%ROWTYPE; operation public.financial_operations%ROWTYPE;
        original public.financial_operations%ROWTYPE; receipt public.command_receipts%ROWTYPE;
        cash_sign integer; control_key text; same_asset boolean; member_count integer;
        cash_scale integer; principal_scale integer; fee jsonb; fee_component integer;
        precision_digits integer; fee_amount numeric;
BEGIN
    SELECT * INTO entry FROM public.business_refunds WHERE id=refund_identifier;
    SELECT * INTO payment FROM public.business_payments WHERE id=entry.payment_id;
    SELECT * INTO header FROM public.journals WHERE id=entry.journal_id;
    SELECT * INTO operation FROM public.financial_operations WHERE id=entry.operation_id;
    SELECT * INTO original FROM public.financial_operations WHERE id=payment.operation_id;
    SELECT * INTO receipt FROM public.command_receipts
        WHERE ledger_id=entry.ledger_id AND key=entry.idempotency_key;
    SELECT scale INTO cash_scale FROM public.assets WHERE asset_id=entry.asset_id;
    SELECT scale INTO principal_scale FROM public.assets WHERE asset_id=payment.asset_id;
    SELECT count(*) INTO member_count FROM public.business_refund_returns WHERE refund_id=entry.id;
    cash_sign := CASE payment.direction WHEN 'receipt' THEN -1 ELSE 1 END;
    control_key := CASE payment.direction WHEN 'receipt' THEN 'advance.received'
        ELSE 'advance.paid' END;
    same_asset := entry.asset_id=payment.asset_id;
    IF entry.id IS NULL OR payment.id IS NULL OR header.id IS NULL OR operation.id IS NULL
       OR original.id IS NULL OR payment.ledger_id IS DISTINCT FROM entry.ledger_id
       OR payment.direction NOT IN ('receipt','payout')
       OR original.kind IS DISTINCT FROM 'payment'
       OR original.ledger_id IS DISTINCT FROM entry.ledger_id
       OR original.version IS DISTINCT FROM 1 OR original.status IS DISTINCT FROM 'active'
       OR original.current_journal_id IS DISTINCT FROM payment.journal_id
       OR entry.source_version IS DISTINCT FROM original.version
       OR operation.kind IS DISTINCT FROM 'refund'
       OR operation.ledger_id IS DISTINCT FROM entry.ledger_id
       OR operation.version IS DISTINCT FROM 1 OR operation.status IS DISTINCT FROM 'active'
       OR operation.current_journal_id IS DISTINCT FROM header.id
       OR operation.created_by IS DISTINCT FROM entry.created_by
       OR header.operation_id IS DISTINCT FROM entry.operation_id
       OR header.ledger_id IS DISTINCT FROM entry.ledger_id
       OR header.operation_version IS DISTINCT FROM 1
       OR header.journal_kind IS DISTINCT FROM 'posting' OR header.reverses_journal_id IS NOT NULL
       OR header.transaction_date IS DISTINCT FROM entry.transaction_date
       OR header.recognition_date IS DISTINCT FROM entry.transaction_date
       OR header.description IS DISTINCT FROM entry.reason OR header.revision_reason IS NOT NULL
       OR entry.transaction_date < payment.transaction_date
       OR entry.idempotency_key=payment.idempotency_key
       OR cash_scale IS NULL OR principal_scale IS NULL
       OR trunc(entry.amount,cash_scale) <> entry.amount
       OR trunc(entry.payment_amount,principal_scale) <> entry.payment_amount
       OR (same_asset AND entry.amount<>entry.payment_amount)
       OR current_setting('transaction_isolation') <> 'read committed'
       OR EXISTS (SELECT 1 FROM public.opening_positions WHERE operation_id=operation.id)
       OR receipt.hash_version IS DISTINCT FROM 2 OR receipt.response_status IS DISTINCT FROM 201
       OR receipt.operation_id IS DISTINCT FROM operation.id
       OR receipt.response IS DISTINCT FROM public.coinpup_business_refund_response(entry.id)
       OR member_count>200 OR (member_count>0 AND (
           (SELECT min(line_no) FROM public.business_refund_returns WHERE refund_id=entry.id)<>1
           OR (SELECT max(line_no) FROM public.business_refund_returns WHERE refund_id=entry.id)
               <>member_count))
       OR EXISTS (SELECT 1 FROM public.business_refund_returns x
           LEFT JOIN public.business_allocation_returns r
               ON r.id=x.return_id AND r.payment_id=x.payment_id AND r.ledger_id=x.ledger_id
           WHERE x.refund_id=entry.id AND (r.id IS NULL OR x.payment_id<>entry.payment_id
               OR x.ledger_id<>entry.ledger_id OR r.return_date>entry.transaction_date))
"""
    "       OR (SELECT coalesce(sum(r.payment_amount),0) FROM "
    "public.coinpup_current_refund_principals(entry.ledger_id, ARRAY[payment.id]) r)"
    r"""
          + (SELECT coalesce(sum(a.payment_amount),0) FROM public.business_allocation_lines a
             WHERE a.payment_id=payment.id AND a.ledger_id=entry.ledger_id)
          - (SELECT coalesce(sum(r.payment_amount),0)
             FROM public.business_allocation_return_lines r
             WHERE r.payment_id=payment.id AND r.ledger_id=entry.ledger_id) > payment.amount
       OR (SELECT count(*) FROM public.journal_lines WHERE journal_id=header.id)
           <> (CASE WHEN same_asset THEN 2 ELSE 4 END)+2*jsonb_array_length(entry.fees)
       OR (SELECT count(*) FROM public.journal_lines WHERE journal_id=header.id AND component_no=0)
           <> (CASE WHEN same_asset THEN 2 ELSE 4 END)
       OR EXISTS (SELECT 1 FROM public.journal_lines l WHERE l.journal_id=header.id
           AND l.component_no=0 AND (l.party_id IS DISTINCT FROM payment.party_id
               OR l.document_id IS NOT NULL OR l.document_line_id IS NOT NULL
               OR l.project_id IS NOT NULL OR l.counterparty_entity_id IS NOT NULL
               OR l.dimension_owner_id IS NOT NULL))
       OR (SELECT count(*) FROM public.journal_lines l JOIN public.accounts a ON a.id=l.account_id
           WHERE l.journal_id=header.id AND l.component_no=0 AND l.role='account'
               AND l.account_id=entry.account_id AND a.account_class='money'
               AND l.asset_id=entry.asset_id AND l.amount=cash_sign*entry.amount) <> 1
       OR (SELECT count(*) FROM public.journal_lines l JOIN public.accounts a ON a.id=l.account_id
           WHERE l.journal_id=header.id AND l.component_no=0 AND l.role='account'
               AND a.system_key=control_key AND l.asset_id=payment.asset_id
               AND l.amount=-cash_sign*entry.payment_amount) <> 1
       OR (NOT same_asset AND (
           (SELECT count(*) FROM public.journal_lines l WHERE l.journal_id=header.id
               AND l.component_no=0 AND l.role='exchange' AND l.asset_id=entry.asset_id
               AND l.amount=-cash_sign*entry.amount) <> 1
           OR (SELECT count(*) FROM public.journal_lines l WHERE l.journal_id=header.id
               AND l.component_no=0 AND l.role='exchange' AND l.asset_id=payment.asset_id
               AND l.amount=cash_sign*entry.payment_amount) <> 1)) THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='Business refund shape is invalid',
            CONSTRAINT='ck_business_refund_shape';
    END IF;
    -- Independent fees use the same strict two-leg component shape as payments.
    FOR fee, fee_component IN SELECT value, ordinality FROM jsonb_array_elements(entry.fees)
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
END; $$;
"""
)

_OLD_SHAPE = r"""
CREATE OR REPLACE FUNCTION coinpup_validate_shape_refund(
    journal_identifier uuid, common_result jsonb)
RETURNS void LANGUAGE plpgsql SET search_path=pg_catalog AS $$
BEGIN
    PERFORM public.coinpup_validate_business_refund(
        (SELECT id FROM public.business_refunds WHERE journal_id=journal_identifier));
    IF common_result->>'error_message' IS NOT NULL THEN
        RAISE EXCEPTION USING ERRCODE=common_result->>'error_sqlstate',
            MESSAGE=common_result->>'error_message', CONSTRAINT=common_result->>'error_constraint';
    END IF;
END; $$;
"""

_CLOSED = r"""
CREATE OR REPLACE FUNCTION coinpup_validate_refund_revision(revision_identifier uuid) RETURNS void
LANGUAGE plpgsql SET search_path=pg_catalog AS $$
BEGIN
    RAISE EXCEPTION USING ERRCODE='23514',
        MESSAGE='Refund revision requires complete refund validation',
        CONSTRAINT='ck_refund_revision_shape';
END; $$;
"""

_LIFECYCLE = r"""
CREATE OR REPLACE FUNCTION coinpup_validate_business_refund(refund_identifier uuid) RETURNS void
LANGUAGE plpgsql SECURITY INVOKER SET search_path=pg_catalog AS $$
DECLARE entry public.business_refunds%ROWTYPE; operation public.financial_operations%ROWTYPE;
    payment public.business_payments%ROWTYPE; step public.business_refund_revisions%ROWTYPE;
    reversal public.journals%ROWTYPE; prior_journal uuid; prior_revision uuid;
    expected_version bigint := 1; terminal boolean := false;
BEGIN
    PERFORM public.coinpup_validate_refund_origin_facts(refund_identifier);
    SELECT * INTO entry FROM public.business_refunds WHERE id=refund_identifier;
    SELECT * INTO operation FROM public.financial_operations
        WHERE id=entry.operation_id AND ledger_id=entry.ledger_id;
    SELECT * INTO payment FROM public.business_payments
        WHERE id=entry.payment_id AND ledger_id=entry.ledger_id;
    prior_journal := entry.journal_id;
    FOR step IN SELECT * FROM public.business_refund_revisions
        WHERE refund_id=entry.id ORDER BY operation_version LOOP
        IF terminal OR step.operation_version::bigint<>expected_version+1
           OR step.previous_journal_id IS DISTINCT FROM prior_journal
           OR step.previous_revision_id IS DISTINCT FROM prior_revision
           OR ROW(step.ledger_id,step.payment_id,step.operation_id) IS DISTINCT FROM
                ROW(entry.ledger_id,entry.payment_id,entry.operation_id) THEN
            RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='Refund revision chain is invalid',
                CONSTRAINT='ck_refund_revision_shape';
        END IF;
        SELECT * INTO reversal FROM public.journals WHERE id=step.reversal_journal_id;
        IF ROW(reversal.operation_id,reversal.ledger_id,reversal.operation_version,
               reversal.journal_kind,reversal.reverses_journal_id,reversal.revision_reason)
              IS DISTINCT FROM ROW(entry.operation_id,entry.ledger_id,step.operation_version,
               'reversal'::varchar,prior_journal,step.revision_reason) THEN
            RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='Refund reversal mapping is invalid',
                CONSTRAINT='ck_refund_revision_shape';
        END IF;
        PERFORM public.coinpup_validate_refund_revision_receipt(step.id);
        expected_version := step.operation_version;
        prior_revision := step.id;
        terminal := step.action='cancel';
        IF NOT terminal THEN
            IF NOT EXISTS (SELECT 1 FROM public.journals p
                WHERE p.id=step.replacement_journal_id AND p.ledger_id=entry.ledger_id
                    AND p.operation_id=entry.operation_id AND p.journal_kind='posting'
                    AND p.operation_version=step.operation_version) THEN
                RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='Refund replacement is invalid',
                    CONSTRAINT='ck_refund_revision_shape';
            END IF;
            prior_journal := step.replacement_journal_id;
        END IF;
    END LOOP;
    IF operation.version::bigint IS DISTINCT FROM expected_version
       OR operation.current_journal_id IS DISTINCT FROM prior_journal
       OR operation.status IS DISTINCT FROM (CASE WHEN terminal THEN 'cancelled' ELSE 'active' END)
       OR (SELECT coalesce(sum(r.payment_amount),0)
           FROM public.coinpup_current_refund_principals(entry.ledger_id,ARRAY[payment.id]) r)
          + (SELECT coalesce(sum(a.payment_amount),0) FROM public.business_allocation_lines a
             WHERE a.payment_id=payment.id AND a.ledger_id=entry.ledger_id)
          - (SELECT coalesce(sum(r.payment_amount),0)
             FROM public.business_allocation_return_lines r
             WHERE r.payment_id=payment.id AND r.ledger_id=entry.ledger_id) > payment.amount THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='Business refund shape is invalid',
            CONSTRAINT='ck_business_refund_shape';
    END IF;
    -- This is the nonrecursive 0007 chain, including full reversal proofs and sealing.
    -- The refund posting dispatch below uses only the historical posting leaf.
    IF prior_revision IS NOT NULL THEN
        PERFORM public.coinpup_validate_operation_history(entry.operation_id);
    END IF;
END; $$;

CREATE OR REPLACE FUNCTION coinpup_validate_shape_refund(
    journal_identifier uuid, common_result jsonb)
RETURNS void LANGUAGE plpgsql SECURITY INVOKER SET search_path=pg_catalog AS $$
BEGIN
    PERFORM public.coinpup_validate_refund_posting_fact(journal_identifier,common_result);
END; $$;

CREATE OR REPLACE FUNCTION coinpup_validate_refund_revision(revision_identifier uuid) RETURNS void
LANGUAGE plpgsql SECURITY INVOKER SET search_path=pg_catalog AS $$
DECLARE refund_identifier uuid;
BEGIN
    SELECT refund_id INTO refund_identifier FROM public.business_refund_revisions
        WHERE id=revision_identifier;
    IF NOT FOUND THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='Refund revision chain is invalid',
            CONSTRAINT='ck_refund_revision_shape';
    END IF;
    PERFORM public.coinpup_validate_business_refund(refund_identifier);
END; $$;
"""


def upgrade():
    op.execute("SELECT pg_advisory_xact_lock(18945999704708432)")
    op.execute(_LIFECYCLE)


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
    op.execute(_OLD_REFUND)
    op.execute(_OLD_SHAPE)
    op.execute(_CLOSED)
