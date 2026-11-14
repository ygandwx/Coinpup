"""Read one current payment-side principal per refund in both deferred capacity validators."""

from alembic import op

revision = "20261109_0052"
down_revision = "20261109_0051"
branch_labels = None
depends_on = None

_OLD_ALLOCATION = (
    r"""
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
"""
    + (
        "          + (SELECT coalesce(sum(f.payment_amount),0) FROM "
        "public.business_refunds f WHERE f.payment_id=payment.id AND "
        "f.ledger_id=allocation.ledger_id) > payment.amount THEN"
    )
    + r"""
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='Allocation shape is invalid',
            CONSTRAINT='ck_business_allocation_shape';
    END IF;
    bank_sign := CASE payment.direction WHEN 'receipt' THEN 1 ELSE -1 END;
    advance_key := CASE payment.direction WHEN 'receipt' THEN 'advance.received'
        ELSE 'advance.paid' END;
    document_key := CASE payment.direction WHEN 'receipt' THEN 'receivable.customer'
        ELSE 'payable.supplier' END;
    FOR item IN WITH credit_totals AS MATERIALIZED (
        SELECT c.source_line_id, sum(c.total_amount) AS total_amount
        FROM public.coinpup_current_credit_lines(allocation.ledger_id,ARRAY(
            SELECT document_line_id FROM public.business_allocation_lines
            WHERE allocation_id=allocation.id)) c GROUP BY c.source_line_id
"""
    + (
        "    ) SELECT l.*, credits.total_amount AS current_credit_total, d.state, "
        "d.document_kind, d.party_id, d.issue_date,"
    )
    + r"""
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
        LEFT JOIN credit_totals credits
          ON credits.source_line_id=l.document_line_id
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
                   AND a.ledger_id=allocation.ledger_id)
              + coalesce(item.current_credit_total,0) > item.total_amount
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
)
_ALLOCATION = _OLD_ALLOCATION.replace(
    "FROM public.business_refunds f WHERE f.payment_id=payment.id"
    " AND f.ledger_id=allocation.ledger_id",
    "FROM public.coinpup_current_refund_principals(allocation.ledger_id, ARRAY[payment.id]) f",
)

_OLD_REFUND = r"""
CREATE OR REPLACE FUNCTION coinpup_validate_business_refund(refund_identifier uuid)
RETURNS void LANGUAGE plpgsql SET search_path=pg_catalog AS $$
DECLARE entry public.business_refunds%ROWTYPE; payment public.business_payments%ROWTYPE;
        header public.journals%ROWTYPE; operation public.financial_operations%ROWTYPE;
        original public.financial_operations%ROWTYPE; receipt public.command_receipts%ROWTYPE;
        cash_sign integer; control_key text; same_asset boolean; member_count integer;
        cash_scale integer; principal_scale integer; fee jsonb; fee_component integer;
        precision_digits integer; fee_amount numeric;
BEGIN
    SELECT * INTO entry FROM public.business_refunds WHERE id=refund_identifier;
    SELECT * INTO payment FROM public.business_payments WHERE id=entry.payment_id;
    SELECT * INTO header FROM public.journals WHERE id=entry.journal_id;
    SELECT * INTO operation FROM public.financial_operations WHERE id=entry.operation_id;
    SELECT * INTO original FROM public.financial_operations WHERE id=payment.operation_id;
    SELECT * INTO receipt FROM public.command_receipts
        WHERE ledger_id=entry.ledger_id AND key=entry.idempotency_key;
    SELECT scale INTO cash_scale FROM public.assets WHERE asset_id=entry.asset_id;
    SELECT scale INTO principal_scale FROM public.assets WHERE asset_id=payment.asset_id;
    SELECT count(*) INTO member_count FROM public.business_refund_returns WHERE refund_id=entry.id;
    cash_sign := CASE payment.direction WHEN 'receipt' THEN -1 ELSE 1 END;
    control_key := CASE payment.direction WHEN 'receipt' THEN 'advance.received'
        ELSE 'advance.paid' END;
    same_asset := entry.asset_id=payment.asset_id;
    IF entry.id IS NULL OR payment.id IS NULL OR header.id IS NULL OR operation.id IS NULL
       OR original.id IS NULL OR payment.ledger_id IS DISTINCT FROM entry.ledger_id
       OR payment.direction NOT IN ('receipt','payout')
       OR original.kind IS DISTINCT FROM 'payment'
       OR original.ledger_id IS DISTINCT FROM entry.ledger_id
       OR original.version IS DISTINCT FROM 1 OR original.status IS DISTINCT FROM 'active'
       OR original.current_journal_id IS DISTINCT FROM payment.journal_id
       OR entry.source_version IS DISTINCT FROM original.version
       OR operation.kind IS DISTINCT FROM 'refund'
       OR operation.ledger_id IS DISTINCT FROM entry.ledger_id
       OR operation.version IS DISTINCT FROM 1 OR operation.status IS DISTINCT FROM 'active'
       OR operation.current_journal_id IS DISTINCT FROM header.id
       OR operation.created_by IS DISTINCT FROM entry.created_by
       OR header.operation_id IS DISTINCT FROM entry.operation_id
       OR header.ledger_id IS DISTINCT FROM entry.ledger_id
       OR header.operation_version IS DISTINCT FROM 1
       OR header.journal_kind IS DISTINCT FROM 'posting' OR header.reverses_journal_id IS NOT NULL
       OR header.transaction_date IS DISTINCT FROM entry.transaction_date
       OR header.recognition_date IS DISTINCT FROM entry.transaction_date
       OR header.description IS DISTINCT FROM entry.reason OR header.revision_reason IS NOT NULL
       OR entry.transaction_date < payment.transaction_date
       OR entry.idempotency_key=payment.idempotency_key
       OR cash_scale IS NULL OR principal_scale IS NULL
       OR trunc(entry.amount,cash_scale) <> entry.amount
       OR trunc(entry.payment_amount,principal_scale) <> entry.payment_amount
       OR (same_asset AND entry.amount<>entry.payment_amount)
       OR current_setting('transaction_isolation') <> 'read committed'
       OR EXISTS (SELECT 1 FROM public.opening_positions WHERE operation_id=operation.id)
       OR receipt.hash_version IS DISTINCT FROM 2 OR receipt.response_status IS DISTINCT FROM 201
       OR receipt.operation_id IS DISTINCT FROM operation.id
       OR receipt.response IS DISTINCT FROM public.coinpup_business_refund_response(entry.id)
       OR member_count>200 OR (member_count>0 AND (
           (SELECT min(line_no) FROM public.business_refund_returns WHERE refund_id=entry.id)<>1
           OR (SELECT max(line_no) FROM public.business_refund_returns WHERE refund_id=entry.id)
               <>member_count))
       OR EXISTS (SELECT 1 FROM public.business_refund_returns x
           LEFT JOIN public.business_allocation_returns r
               ON r.id=x.return_id AND r.payment_id=x.payment_id AND r.ledger_id=x.ledger_id
           WHERE x.refund_id=entry.id AND (r.id IS NULL OR x.payment_id<>entry.payment_id
               OR x.ledger_id<>entry.ledger_id OR r.return_date>entry.transaction_date))
       OR (SELECT coalesce(sum(r.payment_amount),0) FROM public.business_refunds r
           WHERE r.payment_id=payment.id AND r.ledger_id=entry.ledger_id)
          + (SELECT coalesce(sum(a.payment_amount),0) FROM public.business_allocation_lines a
             WHERE a.payment_id=payment.id AND a.ledger_id=entry.ledger_id)
          - (SELECT coalesce(sum(r.payment_amount),0)
             FROM public.business_allocation_return_lines r
             WHERE r.payment_id=payment.id AND r.ledger_id=entry.ledger_id) > payment.amount
       OR (SELECT count(*) FROM public.journal_lines WHERE journal_id=header.id)
           <> (CASE WHEN same_asset THEN 2 ELSE 4 END)+2*jsonb_array_length(entry.fees)
       OR (SELECT count(*) FROM public.journal_lines WHERE journal_id=header.id AND component_no=0)
           <> (CASE WHEN same_asset THEN 2 ELSE 4 END)
       OR EXISTS (SELECT 1 FROM public.journal_lines l WHERE l.journal_id=header.id
           AND l.component_no=0 AND (l.party_id IS DISTINCT FROM payment.party_id
               OR l.document_id IS NOT NULL OR l.document_line_id IS NOT NULL
               OR l.project_id IS NOT NULL OR l.counterparty_entity_id IS NOT NULL
               OR l.dimension_owner_id IS NOT NULL))
       OR (SELECT count(*) FROM public.journal_lines l JOIN public.accounts a ON a.id=l.account_id
           WHERE l.journal_id=header.id AND l.component_no=0 AND l.role='account'
               AND l.account_id=entry.account_id AND a.account_class='money'
               AND l.asset_id=entry.asset_id AND l.amount=cash_sign*entry.amount) <> 1
       OR (SELECT count(*) FROM public.journal_lines l JOIN public.accounts a ON a.id=l.account_id
           WHERE l.journal_id=header.id AND l.component_no=0 AND l.role='account'
               AND a.system_key=control_key AND l.asset_id=payment.asset_id
               AND l.amount=-cash_sign*entry.payment_amount) <> 1
       OR (NOT same_asset AND (
           (SELECT count(*) FROM public.journal_lines l WHERE l.journal_id=header.id
               AND l.component_no=0 AND l.role='exchange' AND l.asset_id=entry.asset_id
               AND l.amount=-cash_sign*entry.amount) <> 1
           OR (SELECT count(*) FROM public.journal_lines l WHERE l.journal_id=header.id
               AND l.component_no=0 AND l.role='exchange' AND l.asset_id=payment.asset_id
               AND l.amount=cash_sign*entry.payment_amount) <> 1)) THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='Business refund shape is invalid',
            CONSTRAINT='ck_business_refund_shape';
    END IF;
    -- Independent fees use the same strict two-leg component shape as payments.
    FOR fee, fee_component IN SELECT value, ordinality FROM jsonb_array_elements(entry.fees)
        WITH ORDINALITY LOOP
        IF jsonb_typeof(fee) IS DISTINCT FROM 'object' THEN
            RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='Refund fee shape is invalid',
                CONSTRAINT='ck_business_refund_shape';
        END IF;
        IF fee-ARRAY['account_id','asset_id','amount','category_id'] <> '{}'::jsonb
           OR jsonb_typeof(fee->'account_id') IS DISTINCT FROM 'string'
           OR jsonb_typeof(fee->'asset_id') IS DISTINCT FROM 'string'
           OR jsonb_typeof(fee->'amount') IS DISTINCT FROM 'string'
           OR jsonb_typeof(fee->'category_id') IS DISTINCT FROM 'string' THEN
            RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='Refund fee shape is invalid',
                CONSTRAINT='ck_business_refund_shape';
        END IF;
        SELECT scale INTO precision_digits FROM public.assets WHERE asset_id=fee->>'asset_id';
        IF precision_digits IS NULL OR fee->>'amount' !~ '^(0|[1-9][0-9]{0,19})(\.[0-9]{1,18})?$'
           OR length(split_part(fee->>'amount','.',2)) > precision_digits THEN
            RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='Refund fee quantity is invalid',
                CONSTRAINT='ck_business_refund_shape';
        END IF;
        fee_amount := (fee->>'amount')::numeric;
        IF fee_amount<=0 OR fee_amount>=100000000000000000000
           OR (SELECT count(*) FROM public.journal_lines
               WHERE journal_id=header.id AND component_no=fee_component) <> 2
           OR EXISTS (SELECT 1 FROM public.journal_lines l WHERE l.journal_id=header.id
               AND l.component_no=fee_component AND (l.asset_id IS DISTINCT FROM fee->>'asset_id'
                   OR l.party_id IS NOT NULL OR l.document_id IS NOT NULL
                   OR l.document_line_id IS NOT NULL OR l.project_id IS NOT NULL
                   OR l.counterparty_entity_id IS NOT NULL OR l.dimension_owner_id IS NOT NULL))
           OR (SELECT count(*) FROM public.journal_lines l
               JOIN public.accounts a ON a.id=l.account_id
               WHERE l.journal_id=header.id AND l.component_no=fee_component AND l.role='account'
                   AND a.account_class='money' AND l.account_id::text=fee->>'account_id'
                   AND l.amount=-fee_amount) <> 1
           OR (SELECT count(*) FROM public.journal_lines l
               WHERE l.journal_id=header.id AND l.component_no=fee_component AND l.role='expense'
                   AND l.category_id::text=fee->>'category_id' AND l.amount=fee_amount) <> 1 THEN
            RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='Refund fee shape is invalid',
                CONSTRAINT='ck_business_refund_shape';
        END IF;
    END LOOP;
END; $$;
"""
_REFUND = _OLD_REFUND.replace(
    "FROM public.business_refunds r\n           WHERE r.payment_id=payment.id"
    " AND r.ledger_id=entry.ledger_id",
    "FROM public.coinpup_current_refund_principals(entry.ledger_id, ARRAY[payment.id]) r",
)


def upgrade():
    op.execute("SELECT pg_advisory_xact_lock(18945999704708432)")
    op.execute(_ALLOCATION)
    op.execute(_REFUND)


def downgrade():
    op.execute("SELECT pg_advisory_xact_lock(18945999704708432)")
    op.execute(
        "LOCK TABLE public.business_refund_revisions, public.business_refund_revision_returns "
        "IN ACCESS EXCLUSIVE MODE"
    )
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM public.business_refund_revisions)
                OR EXISTS (SELECT 1 FROM public.business_refund_revision_returns)
                OR EXISTS (SELECT 1 FROM public.change_log WHERE
                    entity_type IN ('business_refund_revisions','business_refund_revision_returns')
                    OR (entity_type='business_refunds' AND entity_version>1)) THEN
                RAISE EXCEPTION USING ERRCODE='23514',
                    MESSAGE='Refund revision history prevents downgrade',
                    CONSTRAINT='ck_refund_revision_downgrade';
            END IF;
        END; $$;
    """)
    op.execute(_OLD_REFUND)
    op.execute(_OLD_ALLOCATION)
