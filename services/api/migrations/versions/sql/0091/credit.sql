CREATE OR REPLACE FUNCTION public.coinpup_validate_business_credit(credit_identifier uuid)
 RETURNS void
 LANGUAGE plpgsql
 SET search_path TO 'pg_catalog'
AS $function$
DECLARE entry public.business_credits%ROWTYPE; item record; source_ids uuid[];
BEGIN
    PERFORM public.coinpup_validate_credit_origin_facts(credit_identifier);
    -- An untouched original group retains its pre-revision error contract.
    IF NOT EXISTS (SELECT 1 FROM public.business_credit_revisions WHERE credit_id=credit_identifier)
       AND EXISTS (SELECT 1 FROM public.business_credit_lines l
           LEFT JOIN public.financial_operations o ON o.id=l.operation_id
           WHERE l.credit_id=credit_identifier AND (o.version IS DISTINCT FROM 1
               OR o.status IS DISTINCT FROM 'active'
               OR o.current_journal_id IS DISTINCT FROM l.journal_id)) THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='Business credit line shape is invalid',
            CONSTRAINT='ck_business_credit_shape';
    END IF;
    PERFORM public.coinpup_validate_credit_group_transitions(credit_identifier);
    SELECT * INTO entry FROM public.business_credits WHERE id=credit_identifier;
    FOR item IN SELECT journal_id FROM public.coinpup_credit_posting_facts(
        entry.ledger_id,ARRAY[entry.id],NULL) WHERE group_version>1 LOOP
        PERFORM public.coinpup_validate_credit_posting_fact(item.journal_id,
            public.coinpup_validate_journal_common(item.journal_id));
    END LOOP;
    FOR item IN SELECT id FROM public.business_credit_revisions
        WHERE credit_id=entry.id ORDER BY group_version LOOP
        PERFORM public.coinpup_validate_credit_revision_receipt(item.id);
    END LOOP;
    SELECT array_agg(source_line_id) INTO source_ids FROM (
        SELECT source_line_id FROM public.business_credit_lines WHERE credit_id=entry.id
        UNION SELECT source_line_id FROM public.business_credit_revision_members
            WHERE credit_id=entry.id
    ) sources;
    FOR item IN WITH return_sources AS MATERIALIZED (
        SELECT a.id, a.document_line_id FROM public.business_allocation_lines a
        WHERE a.ledger_id=entry.ledger_id AND a.document_line_id =ANY(source_ids)
    ), return_totals AS MATERIALIZED (
        SELECT s.document_line_id, sum(r.document_amount) AS amount
        FROM public.coinpup_current_return_members(entry.ledger_id,
            ARRAY(SELECT id FROM return_sources)) r
        JOIN return_sources s ON s.id=r.source_allocation_line_id
        GROUP BY s.document_line_id
    ), credit_totals AS MATERIALIZED (
        SELECT c.source_line_id, sum(c.net_amount) AS net_amount,
            sum(c.tax_amount) AS tax_amount, sum(c.total_amount) AS total_amount
        FROM public.coinpup_current_credit_lines(entry.ledger_id,source_ids) c
        GROUP BY c.source_line_id
    ) SELECT s.*, returned.amount AS returned_document_amount, coalesce(credits.net_amount,0) AS current_credit_net,
        coalesce(credits.tax_amount,0) AS current_credit_tax,
        coalesce(credits.total_amount,0) AS current_credit_total
        FROM public.business_document_lines s
        LEFT JOIN return_totals returned ON returned.document_line_id=s.id LEFT JOIN credit_totals credits ON credits.source_line_id=s.id
        WHERE s.id=ANY(source_ids) AND s.document_id=entry.document_id
            AND s.ledger_id=entry.ledger_id LOOP
        IF item.current_credit_net>item.net_amount OR item.current_credit_tax>item.tax_amount
           OR item.current_credit_total
           + (SELECT coalesce(sum(a.document_amount),0) FROM public.business_allocation_lines a
              WHERE a.document_line_id=item.id AND a.ledger_id=entry.ledger_id)
           - coalesce(item.returned_document_amount,0)>item.total_amount
        THEN
            RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='Business credit line shape is invalid',
                CONSTRAINT='ck_business_credit_shape';
        END IF;
    END LOOP;
END; $function$;
