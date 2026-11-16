CREATE OR REPLACE FUNCTION public.coinpup_validate_business_allocation_return(return_identifier uuid)
 RETURNS void
 LANGUAGE plpgsql
 SET search_path TO 'pg_catalog'
AS $function$
DECLARE entry public.business_allocation_returns%ROWTYPE;
        payment public.business_payments%ROWTYPE;
        receipt public.command_receipts%ROWTYPE;
        item record;
        expected_response jsonb;
        member_count integer;
        anchor uuid;
        bank_sign integer;
        advance_key text;
        document_key text;
        same_asset boolean;
        payment_scale integer;
        document_scale integer;
        source_payment_units numeric;
        source_document_units numeric;
        returned_payment_units numeric;
        returned_document_units numeric;
        previous_payment_units numeric;
        previous_document_units numeric;
        expected_payment_units numeric;
BEGIN
    SELECT * INTO entry FROM public.business_allocation_returns WHERE id=return_identifier;
    IF entry.id IS NULL THEN
        RAISE EXCEPTION USING ERRCODE='23514',
            MESSAGE='Allocation return history is incomplete',
            CONSTRAINT='ck_business_allocation_return_shape';
    END IF;
    SELECT * INTO payment FROM public.business_payments WHERE id=entry.payment_id;
    SELECT count(*) INTO member_count FROM public.business_allocation_return_lines
        WHERE return_id=entry.id AND ledger_id=entry.ledger_id;
    SELECT operation_id INTO anchor FROM public.business_allocation_return_lines
        WHERE return_id=entry.id AND ledger_id=entry.ledger_id AND line_no=1;
    expected_response := public.coinpup_business_allocation_return_response(entry.id);
    SELECT * INTO receipt FROM public.command_receipts
        WHERE ledger_id=entry.ledger_id AND key=entry.idempotency_key;
    IF payment.id IS NULL OR payment.ledger_id IS DISTINCT FROM entry.ledger_id
       OR payment.direction NOT IN ('receipt','payout')
       OR entry.idempotency_key=payment.idempotency_key
       OR member_count NOT BETWEEN 1 AND 200
       OR (SELECT min(line_no) FROM public.business_allocation_return_lines
           WHERE return_id=entry.id) IS DISTINCT FROM 1
       OR (SELECT max(line_no) FROM public.business_allocation_return_lines
           WHERE return_id=entry.id) IS DISTINCT FROM member_count
       OR receipt.hash_version IS DISTINCT FROM 2
       OR receipt.response_status IS DISTINCT FROM 201
       OR receipt.operation_id IS DISTINCT FROM anchor
       OR receipt.response IS DISTINCT FROM expected_response
       OR current_setting('transaction_isolation') <> 'read committed' THEN
        RAISE EXCEPTION USING ERRCODE='23514',
            MESSAGE='Allocation return shape is invalid',
            CONSTRAINT='ck_business_allocation_return_shape';
    END IF;
    bank_sign := CASE payment.direction WHEN 'receipt' THEN 1 ELSE -1 END;
    advance_key := CASE payment.direction WHEN 'receipt' THEN 'advance.received'
        ELSE 'advance.paid' END;
    document_key := CASE payment.direction WHEN 'receipt' THEN 'receivable.customer'
        ELSE 'payable.supplier' END;
    FOR item IN
        SELECT l.*, a.allocation_id, a.payment_id AS source_payment_id,
            a.document_id, a.document_line_id, ag.payment_id AS allocation_payment_id,
            a.payment_amount AS source_payment_amount,
            a.document_amount AS source_document_amount,
            a.operation_id AS source_operation_id,
            a.journal_id AS source_journal_id,
            ag.allocation_date, ag.created_by AS allocation_actor,
            d.state, d.document_kind, d.party_id AS document_party,
            d.issue_date, d.asset_id AS document_asset,
            s.project_id, s.description AS source_description, s.total_amount,
            p.party_id AS payment_party, p.direction, p.asset_id AS payment_asset,
            p.created_by AS payment_actor, p.transaction_date AS payment_date,
            p.amount AS payment_total, p.operation_id AS payment_operation,
            p.journal_id AS payment_journal,
            sf.kind AS source_kind, sf.status AS source_status,
            sf.version AS source_version_now, sf.current_journal_id AS source_current_journal,
            sf.created_by AS source_actor, sf.ledger_id AS source_ledger,
            sj.operation_version AS source_journal_version,
            sj.journal_kind AS source_journal_kind,
            sj.reverses_journal_id AS source_reversal,
            pf.kind AS payment_kind, pf.status AS payment_status,
            pf.current_journal_id AS payment_current_journal,
            pa.scale AS payment_asset_scale, da.scale AS document_asset_scale,
            ro.kind AS return_operation_kind, ro.status AS return_operation_status,
            ro.version AS return_operation_version,
            ro.current_journal_id AS return_current_journal,
            ro.created_by AS return_actor, ro.ledger_id AS return_ledger,
            rj.operation_id AS return_journal_operation,
            rj.operation_version AS return_journal_version,
            rj.journal_kind AS return_journal_kind,
            rj.reverses_journal_id AS return_reversal,
            rj.transaction_date AS return_transaction_date,
            rj.recognition_date AS return_recognition_date,
            rj.description AS return_description,
            rj.revision_reason AS return_revision_reason,
            rj.ledger_id AS return_journal_ledger
        FROM public.business_allocation_return_lines l
        LEFT JOIN public.business_allocation_lines a
          ON a.id=l.source_allocation_line_id AND a.ledger_id=l.ledger_id
        LEFT JOIN public.business_allocations ag
          ON ag.id=a.allocation_id AND ag.ledger_id=a.ledger_id
        LEFT JOIN public.business_payments p
          ON p.id=a.payment_id AND p.ledger_id=a.ledger_id
        LEFT JOIN public.business_documents d
          ON d.id=a.document_id AND d.ledger_id=a.ledger_id
        LEFT JOIN public.business_document_lines s
          ON s.id=a.document_line_id AND s.document_id=a.document_id AND s.ledger_id=a.ledger_id
        LEFT JOIN public.financial_operations sf
          ON sf.id=a.operation_id AND sf.ledger_id=a.ledger_id
        LEFT JOIN public.journals sj ON sj.id=a.journal_id AND sj.ledger_id=a.ledger_id
        LEFT JOIN public.financial_operations pf
          ON pf.id=p.operation_id AND pf.ledger_id=p.ledger_id
        LEFT JOIN public.assets pa ON pa.asset_id=p.asset_id
        LEFT JOIN public.assets da ON da.asset_id=d.asset_id
        LEFT JOIN public.financial_operations ro
          ON ro.id=l.operation_id AND ro.ledger_id=l.ledger_id
        LEFT JOIN public.journals rj ON rj.id=l.journal_id AND rj.ledger_id=l.ledger_id
        WHERE l.return_id=entry.id AND l.ledger_id=entry.ledger_id
        ORDER BY l.line_no
    LOOP
        same_asset := item.payment_asset=item.document_asset;
        IF item.allocation_id IS NULL OR item.payment_id IS DISTINCT FROM entry.payment_id
           OR item.source_payment_id IS DISTINCT FROM entry.payment_id
           OR item.allocation_payment_id IS DISTINCT FROM entry.payment_id
           OR item.source_ledger IS DISTINCT FROM entry.ledger_id
           OR item.source_kind IS DISTINCT FROM 'allocation'
           OR item.source_status IS DISTINCT FROM 'active'
           OR item.source_version_now IS DISTINCT FROM 1
           OR item.source_version IS DISTINCT FROM item.source_version_now
           OR item.source_current_journal IS DISTINCT FROM item.source_journal_id
           OR item.source_actor IS DISTINCT FROM item.allocation_actor
           OR item.source_actor IS DISTINCT FROM entry.created_by
           OR item.source_journal_kind IS DISTINCT FROM 'posting'
           OR item.source_reversal IS NOT NULL
           OR item.source_journal_version IS DISTINCT FROM 1
           OR item.payment_kind IS DISTINCT FROM 'payment'
           OR item.payment_status IS DISTINCT FROM 'active'
           OR item.payment_current_journal IS DISTINCT FROM item.payment_journal
           OR item.allocation_date < greatest(item.payment_date,item.issue_date)
           OR entry.return_date < item.allocation_date
           OR item.state IS DISTINCT FROM 'confirmed'
           OR item.document_party IS DISTINCT FROM item.payment_party
           OR item.document_kind IS DISTINCT FROM
               (CASE item.direction WHEN 'receipt' THEN 'invoice' ELSE 'bill' END)
           OR item.payment_asset_scale IS NULL OR item.document_asset_scale IS NULL
           OR trunc(item.payment_amount,item.payment_asset_scale) <> item.payment_amount
           OR trunc(item.document_amount,item.document_asset_scale) <> item.document_amount
           OR item.source_payment_amount<=0 OR item.source_document_amount<=0
           OR item.payment_amount<=0 OR item.document_amount<=0
           OR (same_asset AND (item.source_payment_amount<>item.source_document_amount
               OR item.payment_amount<>item.document_amount))
           OR item.return_operation_kind IS DISTINCT FROM 'unallocation'
           OR item.return_operation_status IS DISTINCT FROM 'active'
           OR item.return_operation_version IS DISTINCT FROM 1
           OR item.return_current_journal IS DISTINCT FROM item.journal_id
           OR item.return_actor IS DISTINCT FROM entry.created_by
           OR item.return_ledger IS DISTINCT FROM entry.ledger_id
           OR item.return_journal_operation IS DISTINCT FROM item.operation_id
           OR item.return_journal_version IS DISTINCT FROM 1
           OR item.return_journal_kind IS DISTINCT FROM 'posting'
           OR item.return_reversal IS NOT NULL
           OR item.return_transaction_date IS DISTINCT FROM entry.return_date
           OR item.return_recognition_date IS DISTINCT FROM entry.return_date
           OR item.return_description IS DISTINCT FROM entry.reason
           OR item.return_revision_reason IS NOT NULL
           OR item.return_journal_ledger IS DISTINCT FROM entry.ledger_id
           OR (SELECT count(*) FROM public.journal_lines WHERE journal_id=item.journal_id)
                <> (CASE WHEN same_asset THEN 2 ELSE 4 END)
           OR EXISTS (SELECT 1 FROM public.journal_lines x WHERE x.journal_id=item.journal_id
               AND (x.component_no<>0 OR x.party_id IS DISTINCT FROM item.payment_party
                    OR x.counterparty_entity_id IS NOT NULL OR x.dimension_owner_id IS NOT NULL))
           OR (SELECT count(*) FROM public.journal_lines x
               JOIN public.accounts a ON a.id=x.account_id
               WHERE x.journal_id=item.journal_id AND x.role='account'
                 AND a.system_key=advance_key AND x.asset_id=item.payment_asset
                 AND x.amount=-bank_sign*item.payment_amount
                 AND x.document_id IS NULL AND x.document_line_id IS NULL
                 AND x.project_id IS NULL)<>1
           OR (SELECT count(*) FROM public.journal_lines x
               JOIN public.accounts a ON a.id=x.account_id
               WHERE x.journal_id=item.journal_id AND x.role='account'
                 AND a.system_key=document_key AND x.asset_id=item.document_asset
                 AND x.amount=bank_sign*item.document_amount
                 AND x.document_id IS NOT DISTINCT FROM item.document_id
                 AND x.document_line_id IS NOT DISTINCT FROM item.document_line_id
                 AND x.project_id IS NOT DISTINCT FROM item.project_id)<>1
           OR (NOT same_asset AND ((SELECT count(*) FROM public.journal_lines x
               WHERE x.journal_id=item.journal_id AND x.role='exchange'
                 AND x.asset_id=item.payment_asset AND x.amount=bank_sign*item.payment_amount
                 AND x.document_id IS NULL AND x.document_line_id IS NULL
                 AND x.project_id IS NULL)<>1
               OR (SELECT count(*) FROM public.journal_lines x
                   WHERE x.journal_id=item.journal_id AND x.role='exchange'
                     AND x.asset_id=item.document_asset
                     AND x.amount=-bank_sign*item.document_amount
                     AND x.document_id IS NOT DISTINCT FROM item.document_id
                     AND x.document_line_id IS NOT DISTINCT FROM item.document_line_id
                     AND x.project_id IS NOT DISTINCT FROM item.project_id)<>1)) THEN
            RAISE EXCEPTION USING ERRCODE='23514',
                MESSAGE='Allocation return shape is invalid',
                CONSTRAINT='ck_business_allocation_return_shape';
        END IF;

        source_payment_units := item.source_payment_amount
            * (10::numeric ^ item.payment_asset_scale);
        source_document_units := item.source_document_amount
            * (10::numeric ^ item.document_asset_scale);
        returned_payment_units := item.payment_amount * (10::numeric ^ item.payment_asset_scale);
        returned_document_units := item.document_amount * (10::numeric ^ item.document_asset_scale);
        SELECT coalesce(sum(x.payment_amount),0) * (10::numeric ^ item.payment_asset_scale),
               coalesce(sum(x.document_amount),0) * (10::numeric ^ item.document_asset_scale)
          INTO previous_payment_units, previous_document_units
          FROM public.business_allocation_return_lines x
         WHERE x.source_allocation_line_id=item.source_allocation_line_id
           AND x.ledger_id=entry.ledger_id AND x.id<>item.id;
        IF returned_payment_units + previous_payment_units > source_payment_units
           OR returned_document_units + previous_document_units > source_document_units
           OR item.source_return_no IS DISTINCT FROM
               (SELECT count(*) FROM public.business_allocation_return_lines x
                WHERE x.source_allocation_line_id=item.source_allocation_line_id
                  AND x.ledger_id=entry.ledger_id) THEN
            RAISE EXCEPTION USING ERRCODE='23514',
                MESSAGE='Allocation return exceeds its original mapping',
                CONSTRAINT='ck_business_allocation_return_shape';
        END IF;
        IF returned_document_units + previous_document_units = source_document_units THEN
            expected_payment_units := source_payment_units-previous_payment_units;
        ELSE
            expected_payment_units := div(
                2*returned_document_units*source_payment_units+source_document_units,
                2*source_document_units);
        END IF;
        IF expected_payment_units<=0 OR returned_payment_units<>expected_payment_units
           OR ((returned_payment_units+previous_payment_units=source_payment_units)
               <> (returned_document_units+previous_document_units=source_document_units)) THEN
            RAISE EXCEPTION USING ERRCODE='23514',
                MESSAGE='Allocation return does not follow the original ratio',
                CONSTRAINT='ck_business_allocation_return_shape';
        END IF;
    END LOOP;
END; $function$
