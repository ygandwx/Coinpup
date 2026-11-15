CREATE OR REPLACE FUNCTION public.coinpup_validate_business_refund(refund_identifier uuid)
 RETURNS void
 LANGUAGE plpgsql
 SET search_path TO 'pg_catalog'
AS $function$
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
END; $function$
