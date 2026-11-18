CREATE OR REPLACE FUNCTION public.coinpup_business_credit_response(credit_identifier uuid)
 RETURNS jsonb
 LANGUAGE sql
 STABLE
 SET search_path TO 'pg_catalog'
AS $function$
    SELECT jsonb_build_object(
        'kind','business_credit','id',c.id,'ledger_id',c.ledger_id,
        'document_id',c.document_id,'source_version',c.source_version,
        'asset_id',d.asset_id,'credit_date',c.credit_date::text,
        'postings',coalesce((SELECT jsonb_agg(jsonb_build_object(
            'line_no',l.line_no,'source_line_id',l.source_line_id,
            'source_operation_id',l.source_operation_id,
            'net_amount',l.net_amount::text,'tax_amount',l.tax_amount::text,
            'total_amount',l.total_amount::text,'recognition_date',l.recognition_date::text,
            'operation_id',l.operation_id,'journal_id',l.journal_id
        ) ORDER BY l.line_no) FROM public.business_credit_lines l
        WHERE l.credit_id=c.id AND l.ledger_id=c.ledger_id),'[]'::jsonb))
    FROM public.business_credits c JOIN public.business_documents d
        ON d.id=c.document_id AND d.ledger_id=c.ledger_id WHERE c.id=credit_identifier;
$function$;
