CREATE OR REPLACE FUNCTION public.coinpup_validate_document_revision(ledger_identifier uuid,
    document_identifier uuid,revision_identifier uuid) RETURNS void
LANGUAGE plpgsql SET search_path=pg_catalog SET plan_cache_mode=force_generic_plan AS $$
DECLARE h public.business_document_revisions%ROWTYPE; operation_identifier uuid;
    document_accrual boolean;
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
    FOR operation_identifier,document_accrual IN
        SELECT DISTINCT a.operation_id,o.kind IN ('invoice_accrual','bill_accrual')
        FROM public.business_document_accruals a
        LEFT JOIN public.financial_operations o ON (o.id,o.ledger_id)=(a.operation_id,a.ledger_id)
        WHERE (a.revision_id,a.document_id,a.ledger_id)=(h.id,h.document_id,h.ledger_id)
        ORDER BY a.operation_id LOOP
        -- Existing proofs check every posting, reversal and continuous head, and seal them.
        PERFORM public.coinpup_validate_operation_history(operation_identifier);
        -- The posting-shape proof already checks the document head for these operations.
        -- Keep the explicit check for any other operation kind reaching this proof.
        IF document_accrual IS NOT TRUE THEN
            PERFORM public.coinpup_validate_document_operation_head(operation_identifier);
        END IF;
    END LOOP;
    PERFORM public.coinpup_validate_business_confirmation(h.confirmation_id);
    PERFORM public.coinpup_validate_document_dependencies(h.ledger_id,h.document_id);
END; $$;
