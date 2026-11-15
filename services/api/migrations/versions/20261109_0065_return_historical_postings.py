"""Prove individual posting legs and fixed 201s from immutable allocation return facts."""

from alembic import op

revision = "20261109_0065"
down_revision = "20261109_0064"
branch_labels = None
depends_on = None


_POSTING = r"""
CREATE FUNCTION coinpup_validate_return_posting_fact(journal_identifier uuid, common_result jsonb)
RETURNS void LANGUAGE plpgsql SECURITY INVOKER SET search_path=pg_catalog AS $$
DECLARE f record; j public.journals%ROWTYPE; o public.financial_operations%ROWTYPE;
    source public.business_allocation_lines%ROWTYPE; allocation public.business_allocations%ROWTYPE;
    payment public.business_payments%ROWTYPE; document public.business_documents%ROWTYPE;
    origin public.business_allocation_returns%ROWTYPE; src_op public.financial_operations%ROWTYPE;
    pay_op public.financial_operations%ROWTYPE; src_journal public.journals%ROWTYPE;
    actual_common jsonb; payment_scale integer; document_scale integer;
    same_asset boolean; bank_sign integer; advance_key text; document_key text;
BEGIN
    SELECT * INTO j FROM public.journals WHERE id=journal_identifier;
    BEGIN
        SELECT * INTO STRICT f FROM public.coinpup_return_posting_facts(
            j.ledger_id,NULL,ARRAY[journal_identifier]);
    EXCEPTION WHEN no_data_found OR too_many_rows THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='Allocation return posting is invalid',
            CONSTRAINT='ck_business_allocation_return_shape';
    END;
    SELECT * INTO origin FROM public.business_allocation_returns
        WHERE (id,payment_id,ledger_id)=(f.return_id,f.payment_id,f.ledger_id);
    SELECT * INTO source FROM public.business_allocation_lines
        WHERE (id,payment_id,ledger_id)=(f.source_allocation_line_id,f.payment_id,f.ledger_id);
    SELECT * INTO allocation FROM public.business_allocations
        WHERE (id,payment_id,ledger_id)=(source.allocation_id,f.payment_id,f.ledger_id);
    SELECT * INTO payment FROM public.business_payments
        WHERE (id,ledger_id)=(f.payment_id,f.ledger_id);
    SELECT * INTO document FROM public.business_documents
        WHERE (id,ledger_id)=(source.document_id,f.ledger_id);
    SELECT * INTO o FROM public.financial_operations
        WHERE (id,ledger_id)=(f.operation_id,f.ledger_id);
    SELECT * INTO src_op FROM public.financial_operations
        WHERE (id,ledger_id)=(source.operation_id,f.ledger_id);
    SELECT * INTO pay_op FROM public.financial_operations
        WHERE (id,ledger_id)=(payment.operation_id,f.ledger_id);
    SELECT * INTO src_journal FROM public.journals
        WHERE (id,ledger_id)=(source.journal_id,f.ledger_id);
    SELECT scale INTO payment_scale FROM public.assets WHERE asset_id=f.payment_asset_id;
    SELECT scale INTO document_scale FROM public.assets WHERE asset_id=f.document_asset_id;
    same_asset := f.payment_asset_id=f.document_asset_id;
    bank_sign := CASE f.direction WHEN 'receipt' THEN 1 ELSE -1 END;
    advance_key := CASE f.direction WHEN 'receipt' THEN 'advance.received' ELSE 'advance.paid' END;
    document_key := CASE f.direction WHEN 'receipt' THEN 'receivable.customer'
        ELSE 'payable.supplier' END;
    IF origin.id IS NULL OR source.id IS NULL OR allocation.id IS NULL OR payment.id IS NULL
       OR document.state IS DISTINCT FROM 'confirmed'
       OR document.party_id IS DISTINCT FROM payment.party_id
       OR f.direction NOT IN ('receipt','payout')
       OR document.document_kind IS DISTINCT FROM
            (CASE f.direction WHEN 'receipt' THEN 'invoice' ELSE 'bill' END)
       OR allocation.allocation_date<greatest(payment.transaction_date,document.issue_date)
       OR f.return_date<allocation.allocation_date OR src_op.kind IS DISTINCT FROM 'allocation'
       OR src_op.version IS DISTINCT FROM 1 OR src_op.status IS DISTINCT FROM 'active'
       OR src_op.current_journal_id IS DISTINCT FROM source.journal_id
       OR src_op.created_by IS DISTINCT FROM allocation.created_by
       OR allocation.created_by IS DISTINCT FROM origin.created_by
       OR f.source_version IS DISTINCT FROM src_op.version
       OR src_journal.operation_id IS DISTINCT FROM src_op.id
       OR src_journal.operation_version IS DISTINCT FROM 1
       OR src_journal.journal_kind IS DISTINCT FROM 'posting'
       OR src_journal.reverses_journal_id IS NOT NULL OR pay_op.kind IS DISTINCT FROM 'payment'
       OR pay_op.status IS DISTINCT FROM 'active' OR pay_op.version IS DISTINCT FROM 1
       OR pay_op.current_journal_id IS DISTINCT FROM payment.journal_id
       OR o.kind IS DISTINCT FROM 'unallocation' OR o.created_by IS DISTINCT FROM origin.created_by
       OR f.created_by IS DISTINCT FROM origin.created_by
       OR j.operation_id IS DISTINCT FROM f.operation_id
       OR j.operation_version IS DISTINCT FROM f.operation_version
       OR j.journal_kind IS DISTINCT FROM 'posting' OR j.reverses_journal_id IS NOT NULL
       OR j.transaction_date IS DISTINCT FROM f.return_date
       OR j.recognition_date IS DISTINCT FROM f.return_date
       OR j.description IS DISTINCT FROM f.return_reason
       OR j.revision_reason IS DISTINCT FROM
           (CASE WHEN f.operation_version>1 THEN f.revision_reason END)
       OR EXISTS (SELECT 1 FROM public.opening_positions WHERE operation_id=f.operation_id)
       OR payment_scale IS NULL OR document_scale IS NULL
       OR f.payment_amount<=0 OR f.document_amount<=0
       OR f.payment_amount>source.payment_amount OR f.document_amount>source.document_amount
       OR trunc(f.payment_amount,payment_scale)<>f.payment_amount
       OR trunc(f.document_amount,document_scale)<>f.document_amount
       OR (same_asset AND (f.payment_amount<>f.document_amount
            OR source.payment_amount<>source.document_amount))
       OR (SELECT count(*) FROM public.journal_lines WHERE journal_id=j.id)
           <> (CASE WHEN same_asset THEN 2 ELSE 4 END)
       OR EXISTS (SELECT 1 FROM public.journal_lines l WHERE l.journal_id=j.id
            AND (l.component_no<>0 OR l.party_id IS DISTINCT FROM f.party_id
                OR l.counterparty_entity_id IS NOT NULL OR l.dimension_owner_id IS NOT NULL))
       OR (SELECT count(*) FROM public.journal_lines l JOIN public.accounts a ON a.id=l.account_id
           WHERE l.journal_id=j.id AND l.role='account' AND a.system_key=advance_key
             AND l.asset_id=f.payment_asset_id AND l.amount=-bank_sign*f.payment_amount
             AND l.document_id IS NULL AND l.document_line_id IS NULL AND l.project_id IS NULL)<>1
       OR (SELECT count(*) FROM public.journal_lines l JOIN public.accounts a ON a.id=l.account_id
           WHERE l.journal_id=j.id AND l.role='account' AND a.system_key=document_key
             AND l.asset_id=f.document_asset_id AND l.amount=bank_sign*f.document_amount
             AND l.document_id IS NOT DISTINCT FROM f.document_id
             AND l.document_line_id IS NOT DISTINCT FROM f.document_line_id
             AND l.project_id IS NOT DISTINCT FROM f.project_id)<>1
       OR (NOT same_asset AND ((SELECT count(*) FROM public.journal_lines l
           WHERE l.journal_id=j.id AND l.role='exchange'
             AND l.asset_id=f.payment_asset_id AND l.amount=bank_sign*f.payment_amount
             AND l.document_id IS NULL AND l.document_line_id IS NULL AND l.project_id IS NULL)<>1
           OR (SELECT count(*) FROM public.journal_lines l
               WHERE l.journal_id=j.id AND l.role='exchange'
                 AND l.asset_id=f.document_asset_id AND l.amount=-bank_sign*f.document_amount
                 AND l.document_id IS NOT DISTINCT FROM f.document_id
                 AND l.document_line_id IS NOT DISTINCT FROM f.document_line_id
                 AND l.project_id IS NOT DISTINCT FROM f.project_id)<>1)) THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='Allocation return posting is invalid',
            CONSTRAINT='ck_business_allocation_return_shape';
    END IF;
    actual_common := public.coinpup_validate_journal_common(journal_identifier);
    IF actual_common->>'error_message' IS NOT NULL THEN
        RAISE EXCEPTION USING ERRCODE=actual_common->>'error_sqlstate',
            MESSAGE=actual_common->>'error_message', CONSTRAINT=actual_common->>'error_constraint';
    END IF;
    IF common_result IS DISTINCT FROM actual_common THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='Allocation return posting is invalid',
            CONSTRAINT='ck_business_allocation_return_shape';
    END IF;
END; $$;
"""

_ORIGIN = r"""
CREATE FUNCTION coinpup_validate_return_origin_facts(return_identifier uuid)
RETURNS void LANGUAGE plpgsql SECURITY INVOKER SET search_path=pg_catalog AS $$
DECLARE entry public.business_allocation_returns%ROWTYPE; receipt public.command_receipts%ROWTYPE;
    member record; expected_member integer:=0; anchor uuid;
BEGIN
    SELECT * INTO entry FROM public.business_allocation_returns WHERE id=return_identifier;
    SELECT * INTO receipt FROM public.command_receipts
        WHERE ledger_id=entry.ledger_id AND key=entry.idempotency_key;
    FOR member IN SELECT line_no,operation_id,journal_id
        FROM public.business_allocation_return_lines
        WHERE (return_id,payment_id,ledger_id)=(entry.id,entry.payment_id,entry.ledger_id)
        ORDER BY line_no LOOP
        expected_member := expected_member+1;
        IF expected_member=1 THEN anchor := member.operation_id; END IF;
        IF expected_member>200 OR member.line_no<>expected_member THEN
            RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='Allocation return origin is invalid',
                CONSTRAINT='ck_business_allocation_return_shape';
        END IF;
        PERFORM public.coinpup_validate_return_posting_fact(member.journal_id,
            public.coinpup_validate_journal_common(member.journal_id));
    END LOOP;
    IF entry.id IS NULL OR expected_member=0 OR current_setting('transaction_isolation')
        <> 'read committed' OR receipt.hash_version IS DISTINCT FROM 2
       OR receipt.response_status IS DISTINCT FROM 201
       OR receipt.operation_id IS DISTINCT FROM anchor
       OR entry.idempotency_key=(SELECT idempotency_key FROM public.business_payments
           WHERE (id,ledger_id)=(entry.payment_id,entry.ledger_id))
       OR receipt.response::text IS DISTINCT FROM
           public.coinpup_business_allocation_return_response(entry.id)::text THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='Allocation return origin is invalid',
            CONSTRAINT='ck_business_allocation_return_shape';
    END IF;
END; $$;
"""


def upgrade():
    op.execute("SELECT pg_advisory_xact_lock(18945999704708432)")
    op.execute(_POSTING)
    op.execute(_ORIGIN)


def downgrade():
    op.execute("SELECT pg_advisory_xact_lock(18945999704708432)")
    op.execute(
        "LOCK TABLE public.business_return_revisions, public.business_return_revision_members "
        "IN ACCESS EXCLUSIVE MODE"
    )
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM public.business_return_revisions)
                OR EXISTS (SELECT 1 FROM public.business_return_revision_members)
                OR EXISTS (SELECT 1 FROM public.change_log
                    WHERE entity_type IN
                        ('business_return_revisions','business_return_revision_members')
                        OR (entity_type='business_allocation_returns' AND entity_version>1)) THEN
                RAISE EXCEPTION USING ERRCODE='23514',
                    MESSAGE='Allocation return revision history prevents downgrade',
                    CONSTRAINT='ck_return_revision_downgrade';
            END IF;
        END; $$;
    """)
    op.execute("DROP FUNCTION coinpup_validate_return_origin_facts(uuid)")
    op.execute("DROP FUNCTION coinpup_validate_return_posting_fact(uuid,jsonb)")
