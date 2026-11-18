CREATE OR REPLACE FUNCTION public.coinpup_credit_posting_facts(ledger_identifier uuid, credit_identifiers uuid[], journal_identifiers uuid[] DEFAULT NULL::uuid[])
 RETURNS TABLE(ledger_id uuid, credit_id uuid, document_id uuid, group_version integer, revision_id uuid, source_line_id uuid, source_operation_id uuid, line_no integer, operation_id uuid, operation_version integer, journal_id uuid, net_amount numeric, tax_amount numeric, total_amount numeric, recognition_date date, credit_date date, credit_reason text, posting_revision_reason text)
 LANGUAGE sql
 STABLE
 SET search_path TO 'pg_catalog'
AS $function$
    SELECT c.ledger_id, c.id, c.document_id, 1, NULL::uuid,
        l.source_line_id, l.source_operation_id, l.line_no,
        l.operation_id, 1, l.journal_id, l.net_amount, l.tax_amount, l.total_amount,
        l.recognition_date, c.credit_date, c.reason::text, NULL::text
    FROM public.business_credits c JOIN public.business_credit_lines l
        ON l.credit_id=c.id AND l.document_id=c.document_id AND l.ledger_id=c.ledger_id
    WHERE c.ledger_id=ledger_identifier
        AND (credit_identifiers IS NOT NULL OR journal_identifiers IS NOT NULL)
        AND (credit_identifiers IS NULL OR c.id=ANY(credit_identifiers))
        AND (journal_identifiers IS NULL OR l.journal_id=ANY(journal_identifiers))
    UNION ALL
    SELECT r.ledger_id, r.credit_id, r.document_id, r.group_version, r.id,
        m.source_line_id, m.source_operation_id, m.line_no,
        m.operation_id, m.operation_version, m.journal_id,
        m.net_amount, m.tax_amount, m.total_amount, m.recognition_date,
        r.credit_date, r.credit_reason::text,
        CASE WHEN m.action='correct' THEN r.revision_reason::text ELSE NULL::text END
    FROM public.business_credit_revisions r JOIN public.business_credit_revision_members m
        ON m.revision_id=r.id AND m.credit_id=r.credit_id AND m.document_id=r.document_id
            AND m.ledger_id=r.ledger_id
    WHERE r.ledger_id=ledger_identifier AND m.action IN ('create','correct')
        AND (credit_identifiers IS NOT NULL OR journal_identifiers IS NOT NULL)
        AND (credit_identifiers IS NULL OR r.credit_id=ANY(credit_identifiers))
        AND (journal_identifiers IS NULL OR m.journal_id=ANY(journal_identifiers));
$function$;
