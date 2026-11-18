CREATE OR REPLACE FUNCTION public.coinpup_validate_return_source_sequences(ledger_identifier uuid, source_identifiers uuid[])
 RETURNS void
 LANGUAGE plpgsql
 STABLE
 SET search_path TO 'pg_catalog'
AS $function$
BEGIN
    IF ledger_identifier IS NULL OR coalesce(cardinality(source_identifiers),0)=0 OR EXISTS (
        SELECT 1 FROM unnest(source_identifiers) AS input(id)
        WHERE NOT EXISTS (SELECT 1 FROM public.business_allocation_lines a
            WHERE (a.id,a.ledger_id)=(input.id,ledger_identifier))
    ) THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='Return source sequence is invalid',
            CONSTRAINT='ck_return_revision_source_sequence';
    END IF;
    IF EXISTS (
        WITH slots AS MATERIALIZED (
            SELECT * FROM public.coinpup_return_source_slots(ledger_identifier,source_identifiers)
        )
        SELECT 1 FROM slots b WHERE b.birth_events<>1 OR b.assigned_no<>b.source_return_no
          OR (b.revision_id IS NOT NULL AND (b.declared_predecessor IS DISTINCT FROM b.predecessor
            OR EXISTS (SELECT 1 FROM public.business_allocation_return_lines original
                WHERE original.operation_id=b.operation_id)))
        UNION ALL
        SELECT 1 FROM public.business_return_revision_members m LEFT JOIN slots b
          ON (b.source_id,b.return_id,b.operation_id,b.source_return_no)=
             (m.source_allocation_line_id,m.return_id,m.operation_id,m.source_return_no)
        WHERE m.ledger_id=ledger_identifier AND m.source_allocation_line_id=ANY(source_identifiers)
          AND (b.operation_id IS NULL OR m.source_predecessor_operation_id
              IS DISTINCT FROM b.predecessor)
    ) THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='Return source sequence is invalid',
            CONSTRAINT='ck_return_revision_source_sequence';
    END IF;
    IF EXISTS (
        WITH shares AS (
            SELECT r.source_allocation_line_id AS source_id,r.source_return_no,r.return_id,
                   p.asset_id=d.asset_id AS same_asset,
                   a.payment_amount*(10::numeric^pa.scale) AS source_p,
                   a.document_amount*(10::numeric^da.scale) AS source_d,
                   r.payment_amount*(10::numeric^pa.scale) AS share_p,
                   r.document_amount*(10::numeric^da.scale) AS share_d
            FROM public.coinpup_current_return_members(ledger_identifier,source_identifiers) r
            JOIN public.business_allocation_lines a
              ON (a.id,a.payment_id,a.ledger_id)
                =(r.source_allocation_line_id,r.payment_id,r.ledger_id)
            JOIN public.business_payments p ON (p.id,p.ledger_id)=(a.payment_id,a.ledger_id)
            JOIN public.business_documents d ON (d.id,d.ledger_id)=(a.document_id,a.ledger_id)
            JOIN public.assets pa ON pa.asset_id=p.asset_id
            JOIN public.assets da ON da.asset_id=d.asset_id
        ), ordered AS (
            SELECT s.*, coalesce(sum(share_p) OVER earlier,0) AS prior_p,
                   coalesce(sum(share_d) OVER earlier,0) AS prior_d,
                   count(*) OVER (PARTITION BY source_id,return_id) AS group_slots
            FROM shares s WINDOW earlier AS (PARTITION BY source_id ORDER BY source_return_no
                ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING)
        )
        SELECT 1 FROM ordered WHERE group_slots<>1 OR public.coinpup_return_share_valid(
            source_p,source_d,prior_p,prior_d,share_p,share_d,same_asset) IS DISTINCT FROM true
    ) THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='Return shares violate the source ratio',
            CONSTRAINT='ck_return_revision_source_sequence';
    END IF;
END; $function$
