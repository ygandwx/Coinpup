CREATE OR REPLACE FUNCTION public.coinpup_validate_document_accrual_fact(journal_identifier uuid) RETURNS void
LANGUAGE plpgsql SET search_path=pg_catalog AS $$
DECLARE source record; posting public.journals%ROWTYPE;
    operation public.financial_operations%ROWTYPE;
    debit numeric; class text; control text; valid boolean;
BEGIN
    WITH sources AS (
        SELECT p.operation_id,1 AS version,c.created_by,d.id AS document_id,d.ledger_id,
            d.document_kind,d.party_id,d.asset_id,d.issue_date,l.id AS line_id,l.project_id,
            l.category_kind,l.category_id,l.total_amount,l.recognition_date,l.description,
            NULL::text AS reason
        FROM public.business_postings p
        JOIN public.business_confirmations c ON (c.id,c.document_id,c.ledger_id)=
            (p.confirmation_id,p.document_id,p.ledger_id)
        JOIN public.business_documents d ON (d.id,d.ledger_id)=(p.document_id,p.ledger_id)
        JOIN public.business_document_lines l ON (l.id,l.document_id,l.ledger_id)=
            (p.line_id,d.id,d.ledger_id)
        WHERE p.journal_id=journal_identifier AND d.state='confirmed' AND NOT l.archived
            AND l.asset_id=d.asset_id
        UNION ALL
        SELECT a.operation_id,a.operation_version,h.created_by,h.document_id,h.ledger_id,
            d.document_kind,h.party_id,h.asset_id,h.issue_date,f.source_line_id,f.project_id,
            f.category_kind,f.category_id,f.total_amount,f.recognition_date,f.description,
            CASE WHEN a.operation_version>1 THEN h.reason END
        FROM public.business_document_accruals a
        JOIN public.business_document_revisions h ON (h.id,h.document_id,h.ledger_id)=
            (a.revision_id,a.document_id,a.ledger_id)
        JOIN public.business_documents d ON (d.id,d.ledger_id)=(h.document_id,h.ledger_id)
        JOIN public.business_document_revision_lines f ON
            (f.id,f.revision_id,f.ledger_id,f.source_line_id)=
            (a.replacement_fact_id,h.id,h.ledger_id,a.source_line_id)
        WHERE a.journal_id=journal_identifier AND h.action='correct'
    )
    SELECT s.*,count(*) OVER () AS matches INTO source FROM sources s;
    IF NOT FOUND OR source.matches<>1 OR (source.total_amount>0) IS NOT TRUE THEN
        RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='Missing or ambiguous accrual source',
            CONSTRAINT='ck_document_accrual_fact';
    END IF;
    SELECT * INTO posting FROM public.journals WHERE id=journal_identifier;
    SELECT * INTO operation FROM public.financial_operations WHERE id=source.operation_id;
    IF (posting.operation_id,posting.ledger_id,posting.operation_version,posting.journal_kind,
        posting.transaction_date,posting.recognition_date,posting.description,posting.revision_reason)
        IS DISTINCT FROM (source.operation_id,source.ledger_id,source.version,'posting'::varchar,
        source.issue_date,source.recognition_date,source.description,source.reason)
        OR posting.reverses_journal_id IS NOT NULL
        OR (operation.ledger_id,operation.kind) IS DISTINCT FROM
            (source.ledger_id,source.document_kind || '_accrual')
        OR (source.version=1 AND operation.created_by IS DISTINCT FROM source.created_by)
        OR EXISTS (SELECT 1 FROM public.opening_positions WHERE operation_id=source.operation_id)
    THEN
        RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='Accrual journal has a different source',
            CONSTRAINT='ck_document_accrual_fact';
    END IF;
    IF source.document_kind='invoice' THEN
        debit:=source.total_amount; class:='income'; control:='receivable.customer';
    ELSE
        debit:=-source.total_amount; class:='expense'; control:='payable.supplier';
    END IF;
    SELECT count(*)=2 AND bool_and((l.ledger_id,l.asset_id,l.party_id,l.document_id,
        l.document_line_id,l.project_id,l.component_no) IS NOT DISTINCT FROM (source.ledger_id,
        source.asset_id,source.party_id,source.document_id,source.line_id,source.project_id,0)
        AND l.counterparty_entity_id IS NULL AND l.dimension_owner_id IS NULL)
        AND count(*) FILTER (WHERE l.role='account' AND a.system_key=control AND l.amount=debit)=1
        AND count(*) FILTER (WHERE l.role=class AND l.category_id=source.category_id
            AND l.amount=-debit)=1 INTO valid
    FROM public.journal_lines l LEFT JOIN public.accounts a
        ON (a.id,a.ledger_id)=(l.account_id,l.ledger_id) WHERE l.journal_id=journal_identifier;
    IF valid IS NOT TRUE OR source.category_kind IS DISTINCT FROM class THEN
        RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='Accrual lines differ from their facts',
            CONSTRAINT='ck_document_accrual_fact';
    END IF;
END; $$;
