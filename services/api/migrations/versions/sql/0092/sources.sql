CREATE OR REPLACE FUNCTION public.coinpup_credit_document_sources(ledger_identifier uuid, credit_identifiers uuid[], journal_identifiers uuid[])
 RETURNS TABLE(credit_id uuid, revision_id uuid, journal_id uuid, ledger_id uuid, document_id uuid, document_version integer, source_line_id uuid, source_operation_id uuid, source_operation_version integer, source_journal_id uuid, document_kind character varying, party_id uuid, asset_id character varying, issue_date date, description character varying, category_id uuid, category_kind character varying, project_id uuid, recognition_date date, net_amount numeric, tax_amount numeric, total_amount numeric)
 LANGUAGE sql
 STABLE
 SET search_path TO 'pg_catalog'
AS $function$
WITH postings AS MATERIALIZED (
    SELECT f.*,CASE WHEN f.revision_id IS NULL THEN c.document_revision_id
        ELSE r.document_revision_id END AS source_revision
    FROM public.coinpup_credit_posting_facts(
        ledger_identifier,credit_identifiers,journal_identifiers) f
    JOIN public.business_credits c ON (c.id,c.document_id,c.ledger_id)=
        (f.credit_id,f.document_id,f.ledger_id)
    LEFT JOIN public.business_credit_revisions r ON (r.id,r.credit_id,r.document_id,r.ledger_id)=
        (f.revision_id,f.credit_id,f.document_id,f.ledger_id)
)
SELECT p.credit_id,p.revision_id,p.journal_id,p.ledger_id,d.id,c.source_version+1,
    l.id,a.operation_id,1,a.journal_id,d.document_kind,d.party_id,d.asset_id,d.issue_date,
    l.description,l.category_id,l.category_kind,l.project_id,l.recognition_date,
    l.net_amount,l.tax_amount,l.total_amount
FROM postings p
JOIN public.business_documents d ON (d.id,d.ledger_id)=(p.document_id,p.ledger_id)
JOIN public.business_confirmations c ON (c.document_id,c.ledger_id)=(d.id,d.ledger_id)
JOIN public.business_document_lines l ON (l.id,l.document_id,l.ledger_id)=
    (p.source_line_id,p.document_id,p.ledger_id)
JOIN public.business_postings a ON (a.line_id,a.document_id,a.ledger_id,a.confirmation_id)=
    (l.id,l.document_id,l.ledger_id,c.id)
WHERE p.source_revision IS NULL AND l.total_amount>0 AND NOT l.archived
UNION ALL
SELECT p.credit_id,p.revision_id,p.journal_id,p.ledger_id,h.document_id,h.document_version,
    l.source_line_id,a.operation_id,a.operation_version,a.journal_id,d.document_kind,
    h.party_id,h.asset_id,h.issue_date,l.description,l.category_id,l.category_kind,l.project_id,
    l.recognition_date,l.net_amount,l.tax_amount,l.total_amount
FROM postings p
JOIN public.business_document_revisions h ON (h.id,h.document_id,h.ledger_id)=
    (p.source_revision,p.document_id,p.ledger_id)
JOIN public.business_documents d ON (d.id,d.ledger_id)=(h.document_id,h.ledger_id)
JOIN public.business_document_revision_lines l ON (l.revision_id,l.source_line_id,
    l.document_id,l.ledger_id)=(h.id,p.source_line_id,h.document_id,h.ledger_id)
JOIN public.business_document_accruals a ON (a.replacement_fact_id,a.revision_id,a.source_line_id,
    a.document_id,a.ledger_id)=(l.id,l.revision_id,l.source_line_id,l.document_id,l.ledger_id)
WHERE h.action='correct' AND l.total_amount>0 AND a.journal_id IS NOT NULL;
$function$;
