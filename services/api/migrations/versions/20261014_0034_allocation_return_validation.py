"""Validate immutable allocation returns and net application capacity."""

from alembic import op

revision = "20261014_0034"
down_revision = "20261014_0033"
branch_labels = None
depends_on = None

_KINDS = (
    "kind IN ('opening', 'income', 'expense', 'transfer', 'exchange', "
    "'invoice_accrual', 'bill_accrual', 'payment', 'allocation', 'unallocation')"
)
_PREVIOUS_KINDS = (
    "kind IN ('opening', 'income', 'expense', 'transfer', 'exchange', "
    "'invoice_accrual', 'bill_accrual', 'payment', 'allocation')"
)

_RESPONSE = r"""
CREATE FUNCTION coinpup_business_allocation_return_response(return_identifier uuid)
RETURNS jsonb LANGUAGE sql STABLE SET search_path=pg_catalog AS $$
    SELECT jsonb_build_object(
        'kind','business_allocation_return','id',r.id,'ledger_id',r.ledger_id,
        'payment_id',r.payment_id,'return_date',r.return_date,
        'postings',coalesce((
            SELECT jsonb_agg(jsonb_build_object(
                'line_no',l.line_no,
                'source_allocation_line_id',l.source_allocation_line_id,
                'source_operation_id',a.operation_id,
                'document_id',a.document_id,
                'document_line_id',a.document_line_id,
                'payment_amount',l.payment_amount::text,
                'document_amount',l.document_amount::text,
                'operation_id',l.operation_id,
                'journal_id',l.journal_id) ORDER BY l.line_no)
            FROM public.business_allocation_return_lines l
            JOIN public.business_allocation_lines a
              ON a.id=l.source_allocation_line_id AND a.ledger_id=l.ledger_id
            WHERE l.return_id=r.id AND l.ledger_id=r.ledger_id
        ),'[]'::jsonb))
    FROM public.business_allocation_returns r WHERE r.id=return_identifier;
$$;
"""

_RETURN_VALIDATION = r"""
CREATE OR REPLACE FUNCTION coinpup_validate_business_allocation_return(return_identifier uuid)
RETURNS void LANGUAGE plpgsql SET search_path=pg_catalog AS $$
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
END; $$;
"""

_ALLOCATION = r"""
CREATE OR REPLACE FUNCTION coinpup_validate_business_allocation(allocation_identifier uuid)
RETURNS void LANGUAGE plpgsql SET search_path=pg_catalog AS $$
DECLARE allocation public.business_allocations%ROWTYPE; payment public.business_payments%ROWTYPE;
    receipt public.command_receipts%ROWTYPE; item record; member_count integer;
    bank_sign integer; advance_key text; document_key text; expected_response jsonb;
    anchor uuid; initial_group boolean; same_asset boolean;
BEGIN
    SELECT * INTO allocation FROM public.business_allocations WHERE id=allocation_identifier;
    SELECT * INTO payment FROM public.business_payments WHERE id=allocation.payment_id;
    SELECT * INTO receipt FROM public.command_receipts
        WHERE ledger_id=allocation.ledger_id AND key=allocation.idempotency_key;
    SELECT count(*) INTO member_count FROM public.business_allocation_lines
        WHERE allocation_id=allocation.id;
    initial_group := payment.initial_allocation_id IS NOT DISTINCT FROM allocation.id;
    anchor := CASE WHEN initial_group THEN payment.operation_id ELSE
        (SELECT operation_id FROM public.business_allocation_lines
            WHERE allocation_id=allocation.id AND line_no=1) END;
    expected_response := CASE WHEN initial_group THEN
        public.coinpup_business_payment_response(payment.id) ELSE
        public.coinpup_business_allocation_response(allocation.id) END;
    IF allocation.id IS NULL OR payment.id IS NULL
       OR current_setting('transaction_isolation') <> 'read committed'
       OR allocation.ledger_id IS DISTINCT FROM payment.ledger_id
       OR allocation.allocation_date < payment.transaction_date
       OR member_count NOT BETWEEN 1 AND 200
       OR (SELECT max(line_no) FROM public.business_allocation_lines
           WHERE allocation_id=allocation.id) IS DISTINCT FROM member_count
       OR receipt.hash_version IS DISTINCT FROM 2 OR receipt.response_status IS DISTINCT FROM 201
       OR receipt.operation_id IS DISTINCT FROM anchor
       OR receipt.response IS DISTINCT FROM expected_response
       OR (initial_group AND (allocation.idempotency_key IS DISTINCT FROM payment.idempotency_key
           OR allocation.created_by IS DISTINCT FROM payment.created_by))
       OR (NOT initial_group AND allocation.idempotency_key=payment.idempotency_key)
       OR (SELECT coalesce(sum(l.payment_amount),0) FROM public.business_allocation_lines l
           WHERE l.payment_id=payment.id AND l.ledger_id=allocation.ledger_id)
          - (SELECT coalesce(sum(r.payment_amount),0)
             FROM public.business_allocation_return_lines r
             WHERE r.payment_id=payment.id AND r.ledger_id=allocation.ledger_id)
          > payment.amount THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='Allocation shape is invalid',
            CONSTRAINT='ck_business_allocation_shape';
    END IF;
    bank_sign := CASE payment.direction WHEN 'receipt' THEN 1 ELSE -1 END;
    advance_key := CASE payment.direction WHEN 'receipt' THEN 'advance.received'
        ELSE 'advance.paid' END;
    document_key := CASE payment.direction WHEN 'receipt' THEN 'receivable.customer'
        ELSE 'payable.supplier' END;
    FOR item IN SELECT l.*, d.state, d.document_kind, d.party_id, d.issue_date,
        d.asset_id AS document_asset, s.project_id, s.description, s.total_amount,
        s.id AS source_id, j.operation_version, j.journal_kind, j.reverses_journal_id,
        j.transaction_date, j.recognition_date, j.description AS journal_description,
        j.revision_reason, f.kind, f.version AS operation_version_now, f.status,
        f.current_journal_id, f.created_by, f.ledger_id AS operation_ledger,
        j.ledger_id AS journal_ledger, j.operation_id AS journal_operation,
        pa.scale AS payment_scale, da.scale AS document_scale
        FROM public.business_allocation_lines l
        LEFT JOIN public.business_documents d ON d.id=l.document_id AND d.ledger_id=l.ledger_id
        LEFT JOIN public.business_document_lines s ON s.id=l.document_line_id
            AND s.document_id=d.id AND s.ledger_id=l.ledger_id
        LEFT JOIN public.financial_operations f ON f.id=l.operation_id
        LEFT JOIN public.journals j ON j.id=l.journal_id
        LEFT JOIN public.assets pa ON pa.asset_id=payment.asset_id
        LEFT JOIN public.assets da ON da.asset_id=d.asset_id
        WHERE l.allocation_id=allocation.id LOOP
        same_asset := payment.asset_id=item.document_asset;
        IF item.source_id IS NULL OR item.state IS DISTINCT FROM 'confirmed'
           OR item.party_id IS DISTINCT FROM payment.party_id
           OR item.document_kind IS DISTINCT FROM
               (CASE payment.direction WHEN 'receipt' THEN 'invoice' ELSE 'bill' END)
           OR allocation.allocation_date < item.issue_date OR item.total_amount<=0
           OR item.payment_scale IS NULL OR item.document_scale IS NULL
           OR trunc(item.payment_amount,item.payment_scale) <> item.payment_amount
           OR trunc(item.document_amount,item.document_scale) <> item.document_amount
           OR (same_asset AND item.payment_amount<>item.document_amount)
           OR (SELECT coalesce(sum(a.document_amount),0)
               FROM public.business_allocation_lines a
               WHERE a.document_line_id=item.document_line_id
                 AND a.ledger_id=allocation.ledger_id)
              - (SELECT coalesce(sum(r.document_amount),0)
                 FROM public.business_allocation_return_lines r
                 JOIN public.business_allocation_lines a
                   ON a.id=r.source_allocation_line_id AND a.ledger_id=r.ledger_id
                 WHERE a.document_line_id=item.document_line_id
                   AND a.ledger_id=allocation.ledger_id) > item.total_amount
           OR item.kind IS DISTINCT FROM 'allocation' OR item.operation_version_now<>1
           OR item.status IS DISTINCT FROM 'active'
           OR item.current_journal_id IS DISTINCT FROM item.journal_id
           OR item.created_by IS DISTINCT FROM allocation.created_by
           OR item.operation_ledger IS DISTINCT FROM allocation.ledger_id
           OR item.journal_ledger IS DISTINCT FROM allocation.ledger_id
           OR item.journal_operation IS DISTINCT FROM item.operation_id
           OR item.operation_version IS DISTINCT FROM 1
           OR item.journal_kind IS DISTINCT FROM 'posting' OR item.reverses_journal_id IS NOT NULL
           OR item.transaction_date IS DISTINCT FROM allocation.allocation_date
           OR item.recognition_date IS DISTINCT FROM allocation.allocation_date
           OR item.journal_description IS DISTINCT FROM item.description
           OR item.revision_reason IS NOT NULL
           OR EXISTS (SELECT 1 FROM public.opening_positions WHERE operation_id=item.operation_id)
           OR NOT EXISTS (SELECT 1 FROM public.business_postings
               WHERE line_id=item.document_line_id AND document_id=item.document_id
                   AND ledger_id=allocation.ledger_id)
           OR (SELECT count(*) FROM public.journal_lines WHERE journal_id=item.journal_id)
               <> (CASE WHEN same_asset THEN 2 ELSE 4 END)
           OR EXISTS (SELECT 1 FROM public.journal_lines r WHERE r.journal_id=item.journal_id
               AND (r.component_no<>0 OR r.party_id IS DISTINCT FROM payment.party_id
                   OR r.counterparty_entity_id IS NOT NULL OR r.dimension_owner_id IS NOT NULL))
           OR (SELECT count(*) FROM public.journal_lines r
               JOIN public.accounts a ON a.id=r.account_id
               WHERE r.journal_id=item.journal_id AND r.role='account' AND a.system_key=advance_key
                   AND r.asset_id=payment.asset_id AND r.amount=bank_sign*item.payment_amount
                   AND r.document_id IS NULL AND r.document_line_id IS NULL
                   AND r.project_id IS NULL)<>1
           OR (SELECT count(*) FROM public.journal_lines r
               JOIN public.accounts a ON a.id=r.account_id
               WHERE r.journal_id=item.journal_id AND r.role='account' AND a.system_key=document_key
                   AND r.asset_id=item.document_asset AND r.amount=-bank_sign*item.document_amount
                   AND r.document_id=item.document_id AND r.document_line_id=item.document_line_id
                   AND r.project_id IS NOT DISTINCT FROM item.project_id)<>1
           OR (NOT same_asset AND ((SELECT count(*) FROM public.journal_lines r
               WHERE r.journal_id=item.journal_id AND r.role='exchange'
                   AND r.asset_id=payment.asset_id
                   AND r.amount=-bank_sign*item.payment_amount AND r.document_id IS NULL
                   AND r.document_line_id IS NULL AND r.project_id IS NULL)<>1
               OR (SELECT count(*) FROM public.journal_lines r
                   WHERE r.journal_id=item.journal_id AND r.role='exchange'
                       AND r.asset_id=item.document_asset
                       AND r.amount=bank_sign*item.document_amount
                       AND r.document_id=item.document_id
                       AND r.document_line_id=item.document_line_id
                       AND r.project_id IS NOT DISTINCT FROM item.project_id)<>1)) THEN
            RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='Allocation line shape is invalid',
                CONSTRAINT='ck_business_allocation_shape';
        END IF;
    END LOOP;
END; $$;
"""

_PREVIOUS_ALLOCATION = r"""
CREATE OR REPLACE FUNCTION coinpup_validate_business_allocation(allocation_identifier uuid)
RETURNS void LANGUAGE plpgsql SET search_path=pg_catalog AS $$
DECLARE allocation public.business_allocations%ROWTYPE; payment public.business_payments%ROWTYPE;
    receipt public.command_receipts%ROWTYPE; item record; member_count integer;
    bank_sign integer; advance_key text; document_key text; expected_response jsonb;
    anchor uuid; initial_group boolean; same_asset boolean;
BEGIN
    SELECT * INTO allocation FROM public.business_allocations WHERE id=allocation_identifier;
    SELECT * INTO payment FROM public.business_payments WHERE id=allocation.payment_id;
    SELECT * INTO receipt FROM public.command_receipts
        WHERE ledger_id=allocation.ledger_id AND key=allocation.idempotency_key;
    SELECT count(*) INTO member_count FROM public.business_allocation_lines
        WHERE allocation_id=allocation.id;
    initial_group := payment.initial_allocation_id IS NOT DISTINCT FROM allocation.id;
    anchor := CASE WHEN initial_group THEN payment.operation_id ELSE
        (SELECT operation_id FROM public.business_allocation_lines
            WHERE allocation_id=allocation.id AND line_no=1) END;
    expected_response := CASE WHEN initial_group THEN
        public.coinpup_business_payment_response(payment.id) ELSE
        public.coinpup_business_allocation_response(allocation.id) END;
    IF allocation.id IS NULL OR payment.id IS NULL
       OR current_setting('transaction_isolation') <> 'read committed'
       OR allocation.ledger_id IS DISTINCT FROM payment.ledger_id
       OR allocation.allocation_date < payment.transaction_date
       OR member_count NOT BETWEEN 1 AND 200
       OR (SELECT max(line_no) FROM public.business_allocation_lines
           WHERE allocation_id=allocation.id) IS DISTINCT FROM member_count
       OR receipt.hash_version IS DISTINCT FROM 2 OR receipt.response_status IS DISTINCT FROM 201
       OR receipt.operation_id IS DISTINCT FROM anchor
       OR receipt.response IS DISTINCT FROM expected_response
       OR (initial_group AND (allocation.idempotency_key IS DISTINCT FROM payment.idempotency_key
           OR allocation.created_by IS DISTINCT FROM payment.created_by))
       OR (NOT initial_group AND allocation.idempotency_key=payment.idempotency_key)
       OR (SELECT sum(payment_amount) FROM public.business_allocation_lines
           WHERE payment_id=payment.id) > payment.amount THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='Allocation shape is invalid',
            CONSTRAINT='ck_business_allocation_shape';
    END IF;
    bank_sign := CASE payment.direction WHEN 'receipt' THEN 1 ELSE -1 END;
    advance_key := CASE payment.direction WHEN 'receipt' THEN 'advance.received'
        ELSE 'advance.paid' END;
    document_key := CASE payment.direction WHEN 'receipt' THEN 'receivable.customer'
        ELSE 'payable.supplier' END;
    FOR item IN SELECT l.*, d.state, d.document_kind, d.party_id, d.issue_date,
        d.asset_id AS document_asset, s.project_id, s.description, s.total_amount,
        s.id AS source_id, j.operation_version, j.journal_kind, j.reverses_journal_id,
        j.transaction_date, j.recognition_date, j.description AS journal_description,
        j.revision_reason, f.kind, f.version AS operation_version_now, f.status,
        f.current_journal_id, f.created_by, f.ledger_id AS operation_ledger,
        j.ledger_id AS journal_ledger, j.operation_id AS journal_operation,
        pa.scale AS payment_scale, da.scale AS document_scale
        FROM public.business_allocation_lines l
        LEFT JOIN public.business_documents d ON d.id=l.document_id AND d.ledger_id=l.ledger_id
        LEFT JOIN public.business_document_lines s ON s.id=l.document_line_id
            AND s.document_id=d.id AND s.ledger_id=l.ledger_id
        LEFT JOIN public.financial_operations f ON f.id=l.operation_id
        LEFT JOIN public.journals j ON j.id=l.journal_id
        LEFT JOIN public.assets pa ON pa.asset_id=payment.asset_id
        LEFT JOIN public.assets da ON da.asset_id=d.asset_id
        WHERE l.allocation_id=allocation.id LOOP
        same_asset := payment.asset_id=item.document_asset;
        IF item.source_id IS NULL OR item.state IS DISTINCT FROM 'confirmed'
           OR item.party_id IS DISTINCT FROM payment.party_id
           OR item.document_kind IS DISTINCT FROM
               (CASE payment.direction WHEN 'receipt' THEN 'invoice' ELSE 'bill' END)
           OR allocation.allocation_date < item.issue_date OR item.total_amount<=0
           OR item.payment_scale IS NULL OR item.document_scale IS NULL
           OR trunc(item.payment_amount,item.payment_scale) <> item.payment_amount
           OR trunc(item.document_amount,item.document_scale) <> item.document_amount
           OR (same_asset AND item.payment_amount<>item.document_amount)
           OR (SELECT sum(document_amount) FROM public.business_allocation_lines
               WHERE document_line_id=item.document_line_id) > item.total_amount
           OR item.kind IS DISTINCT FROM 'allocation' OR item.operation_version_now<>1
           OR item.status IS DISTINCT FROM 'active'
           OR item.current_journal_id IS DISTINCT FROM item.journal_id
           OR item.created_by IS DISTINCT FROM allocation.created_by
           OR item.operation_ledger IS DISTINCT FROM allocation.ledger_id
           OR item.journal_ledger IS DISTINCT FROM allocation.ledger_id
           OR item.journal_operation IS DISTINCT FROM item.operation_id
           OR item.operation_version IS DISTINCT FROM 1
           OR item.journal_kind IS DISTINCT FROM 'posting' OR item.reverses_journal_id IS NOT NULL
           OR item.transaction_date IS DISTINCT FROM allocation.allocation_date
           OR item.recognition_date IS DISTINCT FROM allocation.allocation_date
           OR item.journal_description IS DISTINCT FROM item.description
           OR item.revision_reason IS NOT NULL
           OR EXISTS (SELECT 1 FROM public.opening_positions WHERE operation_id=item.operation_id)
           OR NOT EXISTS (SELECT 1 FROM public.business_postings
               WHERE line_id=item.document_line_id AND document_id=item.document_id
                   AND ledger_id=allocation.ledger_id)
           OR (SELECT count(*) FROM public.journal_lines WHERE journal_id=item.journal_id)
               <> (CASE WHEN same_asset THEN 2 ELSE 4 END)
           OR EXISTS (SELECT 1 FROM public.journal_lines r WHERE r.journal_id=item.journal_id
               AND (r.component_no<>0 OR r.party_id IS DISTINCT FROM payment.party_id
                   OR r.counterparty_entity_id IS NOT NULL OR r.dimension_owner_id IS NOT NULL))
           OR (SELECT count(*) FROM public.journal_lines r
               JOIN public.accounts a ON a.id=r.account_id
               WHERE r.journal_id=item.journal_id AND r.role='account' AND a.system_key=advance_key
                   AND r.asset_id=payment.asset_id AND r.amount=bank_sign*item.payment_amount
                   AND r.document_id IS NULL AND r.document_line_id IS NULL
                   AND r.project_id IS NULL)<>1
           OR (SELECT count(*) FROM public.journal_lines r
               JOIN public.accounts a ON a.id=r.account_id
               WHERE r.journal_id=item.journal_id AND r.role='account' AND a.system_key=document_key
                   AND r.asset_id=item.document_asset AND r.amount=-bank_sign*item.document_amount
                   AND r.document_id=item.document_id AND r.document_line_id=item.document_line_id
                   AND r.project_id IS NOT DISTINCT FROM item.project_id)<>1
           OR (NOT same_asset AND ((SELECT count(*) FROM public.journal_lines r
               WHERE r.journal_id=item.journal_id AND r.role='exchange'
                   AND r.asset_id=payment.asset_id
                   AND r.amount=-bank_sign*item.payment_amount AND r.document_id IS NULL
                   AND r.document_line_id IS NULL AND r.project_id IS NULL)<>1
               OR (SELECT count(*) FROM public.journal_lines r
                   WHERE r.journal_id=item.journal_id AND r.role='exchange'
                       AND r.asset_id=item.document_asset
                       AND r.amount=bank_sign*item.document_amount
                       AND r.document_id=item.document_id
                       AND r.document_line_id=item.document_line_id
                       AND r.project_id IS NOT DISTINCT FROM item.project_id)<>1)) THEN
            RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='Allocation line shape is invalid',
                CONSTRAINT='ck_business_allocation_shape';
        END IF;
    END LOOP;
END; $$;

CREATE OR REPLACE FUNCTION coinpup_validate_shape_allocation(
    journal_identifier uuid, common_result jsonb)
RETURNS void LANGUAGE plpgsql SET search_path=pg_catalog AS $$
BEGIN
    PERFORM public.coinpup_validate_business_allocation(
        (SELECT allocation_id FROM public.business_allocation_lines
            WHERE journal_id=journal_identifier));
    IF common_result->>'error_message' IS NOT NULL THEN
        RAISE EXCEPTION USING ERRCODE=common_result->>'error_sqlstate',
            MESSAGE=common_result->>'error_message', CONSTRAINT=common_result->>'error_constraint';
    END IF;
END; $$;
"""

_PREVIOUS_DISPATCH = r"""
CREATE OR REPLACE FUNCTION coinpup_validate_posting_shape(journal_identifier uuid)
    RETURNS void LANGUAGE plpgsql SET search_path = pg_catalog AS $$
    DECLARE header public.journals%ROWTYPE; operation public.financial_operations%ROWTYPE;
            common_result jsonb;
    BEGIN
        SELECT * INTO header FROM public.journals WHERE id = journal_identifier FOR UPDATE;
        IF NOT FOUND THEN
            RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'Journal is missing',
                CONSTRAINT = 'ck_journal_shape';
        END IF;
        SELECT * INTO operation FROM public.financial_operations WHERE id = header.operation_id;
        IF NOT FOUND OR operation.ledger_id <> header.ledger_id OR
           header.journal_kind <> 'posting' OR header.reverses_journal_id IS NOT NULL THEN
            RAISE EXCEPTION USING ERRCODE = '23514',
                MESSAGE = 'Posting journal identity is invalid', CONSTRAINT = 'ck_journal_shape';
        END IF;
        common_result := public.coinpup_validate_journal_common(journal_identifier);
        CASE operation.kind
            WHEN 'opening' THEN
                PERFORM public.coinpup_validate_shape_opening(journal_identifier, common_result);
            WHEN 'income' THEN
                PERFORM public.coinpup_validate_shape_income(journal_identifier, common_result);
            WHEN 'expense' THEN
                PERFORM public.coinpup_validate_shape_expense(journal_identifier, common_result);
            WHEN 'transfer' THEN
                PERFORM public.coinpup_validate_shape_transfer(journal_identifier, common_result);
            WHEN 'exchange' THEN
                PERFORM public.coinpup_validate_shape_exchange(journal_identifier, common_result);
            WHEN 'invoice_accrual' THEN
                PERFORM public.coinpup_validate_shape_invoice_accrual(
                    journal_identifier, common_result);
            WHEN 'bill_accrual' THEN
                PERFORM public.coinpup_validate_shape_bill_accrual(
                    journal_identifier, common_result);
            WHEN 'payment' THEN
                PERFORM public.coinpup_validate_shape_payment(
                    journal_identifier, common_result);
            WHEN 'allocation' THEN
                PERFORM public.coinpup_validate_shape_allocation(
                    journal_identifier, common_result);
            ELSE
                RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'Unknown operation kind',
                    CONSTRAINT = 'ck_journal_shape';
        END CASE;
    END;
    $$
"""

_UNALLOCATION_SHAPE = r"""
CREATE FUNCTION coinpup_validate_shape_unallocation(journal_identifier uuid, common_result jsonb)
RETURNS void LANGUAGE plpgsql SET search_path=pg_catalog AS $$
DECLARE return_identifier uuid;
BEGIN
    SELECT return_id INTO return_identifier FROM public.business_allocation_return_lines
        WHERE journal_id=journal_identifier;
    PERFORM public.coinpup_validate_business_allocation_return(return_identifier);
    IF common_result->>'error_message' IS NOT NULL THEN
        RAISE EXCEPTION USING ERRCODE=common_result->>'error_sqlstate',
            MESSAGE=common_result->>'error_message', CONSTRAINT=common_result->>'error_constraint';
    END IF;
END; $$;
"""

_DISPATCH = r"""
CREATE OR REPLACE FUNCTION coinpup_validate_posting_shape(journal_identifier uuid)
RETURNS void LANGUAGE plpgsql SET search_path=pg_catalog AS $$
DECLARE header public.journals%ROWTYPE; operation public.financial_operations%ROWTYPE;
        common_result jsonb;
BEGIN
    SELECT * INTO header FROM public.journals WHERE id=journal_identifier FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='Journal is missing',
            CONSTRAINT='ck_journal_shape';
    END IF;
    SELECT * INTO operation FROM public.financial_operations WHERE id=header.operation_id;
    IF NOT FOUND OR operation.ledger_id <> header.ledger_id OR
       header.journal_kind <> 'posting' OR header.reverses_journal_id IS NOT NULL THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='Posting journal identity is invalid',
            CONSTRAINT='ck_journal_shape';
    END IF;
    common_result := public.coinpup_validate_journal_common(journal_identifier);
    CASE operation.kind
        WHEN 'opening' THEN
            PERFORM public.coinpup_validate_shape_opening(journal_identifier, common_result);
        WHEN 'income' THEN
            PERFORM public.coinpup_validate_shape_income(journal_identifier, common_result);
        WHEN 'expense' THEN
            PERFORM public.coinpup_validate_shape_expense(journal_identifier, common_result);
        WHEN 'transfer' THEN
            PERFORM public.coinpup_validate_shape_transfer(journal_identifier, common_result);
        WHEN 'exchange' THEN
            PERFORM public.coinpup_validate_shape_exchange(journal_identifier, common_result);
        WHEN 'invoice_accrual' THEN
            PERFORM public.coinpup_validate_shape_invoice_accrual(
                journal_identifier, common_result);
        WHEN 'bill_accrual' THEN
            PERFORM public.coinpup_validate_shape_bill_accrual(
                journal_identifier, common_result);
        WHEN 'payment' THEN
            PERFORM public.coinpup_validate_shape_payment(journal_identifier, common_result);
        WHEN 'allocation' THEN
            PERFORM public.coinpup_validate_shape_allocation(journal_identifier, common_result);
        WHEN 'unallocation' THEN
            PERFORM public.coinpup_validate_shape_unallocation(
                journal_identifier, common_result);
        ELSE RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='Unknown operation kind',
            CONSTRAINT='ck_journal_shape';
    END CASE;
END; $$;
"""


def _kinds(value):
    op.drop_constraint("ck_financial_operations_kind", "financial_operations", type_="check")
    op.create_check_constraint("ck_financial_operations_kind", "financial_operations", value)


def upgrade():
    op.execute("SELECT pg_advisory_xact_lock(18945999704708432)")
    op.execute(_RESPONSE)
    op.execute(_RETURN_VALIDATION)
    op.execute(_ALLOCATION)
    op.execute(_UNALLOCATION_SHAPE)
    op.execute(_DISPATCH)
    _kinds(_KINDS)


def downgrade():
    op.execute("SELECT pg_advisory_xact_lock(18945999704708432)")
    op.execute(
        "LOCK TABLE business_allocation_returns, business_allocation_return_lines, "
        "business_allocation_lines, business_allocations, financial_operations "
        "IN ACCESS EXCLUSIVE MODE"
    )
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM public.business_allocation_returns)
               OR EXISTS (SELECT 1 FROM public.business_allocation_return_lines)
               OR EXISTS (SELECT 1 FROM public.financial_operations WHERE kind='unallocation')
               OR EXISTS (SELECT 1 FROM public.change_log WHERE entity_type IN
                   ('business_allocation_returns','business_allocation_return_lines')) THEN
                RAISE EXCEPTION USING ERRCODE='23514',
                    MESSAGE='Allocation return validation history prevents downgrade',
                    CONSTRAINT='ck_business_allocation_return_validation_downgrade';
            END IF;
        END; $$;
    """)
    _kinds(_PREVIOUS_KINDS)
    op.execute(_PREVIOUS_DISPATCH)
    op.execute(_PREVIOUS_ALLOCATION)
    op.execute("""
        CREATE OR REPLACE FUNCTION coinpup_validate_business_allocation_return(
            return_identifier uuid)
        RETURNS void LANGUAGE plpgsql SET search_path=pg_catalog AS $$
        BEGIN
            RAISE EXCEPTION USING ERRCODE='23514',
                MESSAGE='Allocation return requires dedicated validation',
                CONSTRAINT='ck_business_allocation_return_shape';
        END; $$;
    """)
    op.execute("DROP FUNCTION coinpup_validate_shape_unallocation(uuid,jsonb)")
    op.execute("DROP FUNCTION coinpup_business_allocation_return_response(uuid)")
