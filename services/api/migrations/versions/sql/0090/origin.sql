CREATE OR REPLACE FUNCTION public.coinpup_validate_credit_origin_facts(credit_identifier uuid)
 RETURNS void
 LANGUAGE plpgsql
 SET search_path TO 'pg_catalog'
AS $function$
DECLARE entry public.business_credits%ROWTYPE; document public.business_documents%ROWTYPE;
    receipt public.command_receipts%ROWTYPE; member_count integer; anchor uuid; item record;
BEGIN
    SELECT * INTO entry FROM public.business_credits WHERE id=credit_identifier;
    SELECT * INTO document FROM public.business_documents WHERE id=entry.document_id;
    SELECT * INTO receipt FROM public.command_receipts
        WHERE ledger_id=entry.ledger_id AND key=entry.idempotency_key;
    SELECT count(*) INTO member_count FROM public.business_credit_lines WHERE credit_id=entry.id;
    SELECT operation_id INTO anchor FROM public.business_credit_lines
        WHERE credit_id=entry.id AND line_no=1;
    IF entry.id IS NULL OR document.id IS NULL
       OR document.ledger_id IS DISTINCT FROM entry.ledger_id
       OR document.state IS DISTINCT FROM 'confirmed'
       OR document.document_kind NOT IN ('invoice','bill')
       OR entry.source_version IS DISTINCT FROM document.version
       OR entry.credit_date < document.issue_date
       OR member_count<1 OR member_count>200 OR anchor IS NULL
       OR (SELECT max(line_no) FROM public.business_credit_lines WHERE credit_id=entry.id)
          <>member_count
       OR current_setting('transaction_isolation') <> 'read committed'
       OR receipt.hash_version IS DISTINCT FROM 2 OR receipt.response_status IS DISTINCT FROM 201
       OR receipt.operation_id IS DISTINCT FROM anchor
       OR receipt.response IS DISTINCT FROM public.coinpup_business_credit_response(entry.id) THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='Business credit shape is invalid',
            CONSTRAINT='ck_business_credit_shape';
    END IF;
    FOR item IN SELECT journal_id FROM public.business_credit_lines WHERE credit_id=entry.id LOOP
        PERFORM public.coinpup_validate_credit_posting_fact(item.journal_id,
            public.coinpup_validate_journal_common(item.journal_id));
    END LOOP;
END; $function$;
