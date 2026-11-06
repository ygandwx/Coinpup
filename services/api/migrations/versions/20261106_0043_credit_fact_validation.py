"""Prepare immutable credit fact checks without changing callers or the closed write gate."""

from alembic import op

revision = "20261106_0043"
down_revision = "20261106_0042"
branch_labels = None
depends_on = None

_POSTING = r"""
CREATE FUNCTION coinpup_validate_credit_posting_fact(
    journal_identifier uuid, common_result jsonb) RETURNS void
LANGUAGE plpgsql SECURITY INVOKER SET search_path=pg_catalog AS $$
DECLARE header public.journals%ROWTYPE; entry public.business_credits%ROWTYPE;
    document public.business_documents%ROWTYPE; source public.business_document_lines%ROWTYPE;
    posted public.business_postings%ROWTYPE; original public.financial_operations%ROWTYPE;
    operation public.financial_operations%ROWTYPE; fact record; fact_count bigint;
    precision_digits integer; category_sign integer; category_role text; control_key text;
    actual_common jsonb;
BEGIN
    SELECT * INTO header FROM public.journals WHERE id=journal_identifier;
    SELECT count(*) INTO fact_count FROM public.coinpup_credit_posting_facts(
        header.ledger_id,NULL,ARRAY[journal_identifier]);
    IF header.id IS NULL OR fact_count<>1 THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='Business credit shape is invalid',
            CONSTRAINT='ck_business_credit_shape';
    END IF;
    SELECT * INTO fact FROM public.coinpup_credit_posting_facts(
        header.ledger_id,NULL,ARRAY[journal_identifier]);
    SELECT * INTO entry FROM public.business_credits WHERE id=fact.credit_id;
    SELECT * INTO document FROM public.business_documents WHERE id=fact.document_id;
    SELECT * INTO source FROM public.business_document_lines
        WHERE id=fact.source_line_id AND document_id=fact.document_id
            AND ledger_id=fact.ledger_id;
    SELECT * INTO posted FROM public.business_postings WHERE line_id=source.id
        AND document_id=document.id AND ledger_id=fact.ledger_id;
    SELECT * INTO original FROM public.financial_operations WHERE id=fact.source_operation_id;
    SELECT * INTO operation FROM public.financial_operations WHERE id=fact.operation_id;
    SELECT scale INTO precision_digits FROM public.assets WHERE asset_id=document.asset_id;
    category_sign := CASE document.document_kind WHEN 'invoice' THEN 1 ELSE -1 END;
    category_role := CASE document.document_kind WHEN 'invoice' THEN 'income' ELSE 'expense' END;
    control_key := CASE document.document_kind WHEN 'invoice' THEN 'receivable.customer'
        ELSE 'payable.supplier' END;
    IF entry.id IS NULL OR document.id IS NULL OR source.id IS NULL OR posted.id IS NULL
       OR entry.document_id IS DISTINCT FROM document.id
       OR entry.ledger_id IS DISTINCT FROM fact.ledger_id
       OR document.ledger_id IS DISTINCT FROM fact.ledger_id
       OR document.state IS DISTINCT FROM 'confirmed'
       OR document.document_kind NOT IN ('invoice','bill')
       OR entry.source_version IS DISTINCT FROM document.version
       OR fact.credit_date < document.issue_date OR precision_digits IS NULL
       OR source.total_amount<=0 OR fact.source_operation_id IS DISTINCT FROM posted.operation_id
       OR original.ledger_id IS DISTINCT FROM fact.ledger_id
       OR original.kind IS DISTINCT FROM (CASE document.document_kind WHEN 'invoice'
           THEN 'invoice_accrual' ELSE 'bill_accrual' END)
       OR original.version IS DISTINCT FROM 1 OR original.status IS DISTINCT FROM 'active'
       OR original.current_journal_id IS DISTINCT FROM posted.journal_id
       OR operation.kind IS DISTINCT FROM (CASE document.document_kind WHEN 'invoice'
           THEN 'invoice_credit' ELSE 'bill_credit' END)
       OR operation.ledger_id IS DISTINCT FROM fact.ledger_id
       OR operation.created_by IS DISTINCT FROM entry.created_by
       OR header.ledger_id IS DISTINCT FROM fact.ledger_id
       OR header.operation_id IS DISTINCT FROM fact.operation_id
       OR header.operation_version IS DISTINCT FROM fact.operation_version
       OR header.journal_kind IS DISTINCT FROM 'posting' OR header.reverses_journal_id IS NOT NULL
       OR header.transaction_date IS DISTINCT FROM fact.credit_date
       OR header.recognition_date IS DISTINCT FROM fact.recognition_date
       OR header.description IS DISTINCT FROM fact.credit_reason
       OR header.revision_reason IS DISTINCT FROM fact.posting_revision_reason
       OR trunc(fact.net_amount,precision_digits)<>fact.net_amount
       OR trunc(fact.tax_amount,precision_digits)<>fact.tax_amount
       OR fact.net_amount>source.net_amount OR fact.tax_amount>source.tax_amount
       OR EXISTS (SELECT 1 FROM public.opening_positions WHERE operation_id=fact.operation_id)
       OR (SELECT count(*) FROM public.journal_lines WHERE journal_id=header.id)<>2
       OR EXISTS (SELECT 1 FROM public.journal_lines l WHERE l.journal_id=header.id AND (
           l.component_no<>0 OR l.asset_id IS DISTINCT FROM document.asset_id
           OR l.party_id IS DISTINCT FROM document.party_id
           OR l.document_id IS DISTINCT FROM document.id
           OR l.document_line_id IS DISTINCT FROM source.id
           OR l.project_id IS DISTINCT FROM source.project_id
           OR l.counterparty_entity_id IS NOT NULL OR l.dimension_owner_id IS NOT NULL))
       OR (SELECT count(*) FROM public.journal_lines l JOIN public.accounts a
           ON a.id=l.account_id AND a.ledger_id=l.ledger_id
           WHERE l.journal_id=header.id AND l.role='account' AND a.system_key=control_key
               AND l.amount=-category_sign*fact.total_amount)<>1
       OR (SELECT count(*) FROM public.journal_lines l WHERE l.journal_id=header.id
           AND l.role=category_role AND l.category_id=source.category_id
           AND l.amount=category_sign*fact.total_amount)<>1 THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='Business credit line shape is invalid',
            CONSTRAINT='ck_business_credit_shape';
    END IF;
    -- Only the real common result proves balance; caller-supplied JSON is not evidence.
    actual_common := public.coinpup_validate_journal_common(journal_identifier);
    IF actual_common->>'error_message' IS NOT NULL THEN
        RAISE EXCEPTION USING ERRCODE=actual_common->>'error_sqlstate',
            MESSAGE=actual_common->>'error_message',
            CONSTRAINT=actual_common->>'error_constraint';
    END IF;
    IF common_result IS DISTINCT FROM actual_common THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='Business credit shape is invalid',
            CONSTRAINT='ck_business_credit_shape';
    END IF;
END; $$;
"""

_ORIGIN = r"""
CREATE FUNCTION coinpup_validate_credit_origin_facts(credit_identifier uuid) RETURNS void
LANGUAGE plpgsql SECURITY INVOKER SET search_path=pg_catalog AS $$
DECLARE entry public.business_credits%ROWTYPE; document public.business_documents%ROWTYPE;
    receipt public.command_receipts%ROWTYPE; member_count integer; anchor uuid; item record;
BEGIN
    SELECT * INTO entry FROM public.business_credits WHERE id=credit_identifier;
    SELECT * INTO document FROM public.business_documents WHERE id=entry.document_id;
    SELECT * INTO receipt FROM public.command_receipts
        WHERE ledger_id=entry.ledger_id AND key=entry.idempotency_key;
    SELECT count(*) INTO member_count FROM public.business_credit_lines WHERE credit_id=entry.id;
    SELECT operation_id INTO anchor FROM public.business_credit_lines
        WHERE credit_id=entry.id AND line_no=1;
    IF entry.id IS NULL OR document.id IS NULL
       OR document.ledger_id IS DISTINCT FROM entry.ledger_id
       OR document.state IS DISTINCT FROM 'confirmed'
       OR document.document_kind NOT IN ('invoice','bill')
       OR entry.source_version IS DISTINCT FROM document.version
       OR entry.credit_date < document.issue_date
       OR member_count<1 OR member_count>200 OR anchor IS NULL
       OR (SELECT max(line_no) FROM public.business_credit_lines WHERE credit_id=entry.id)
          <>member_count
       OR current_setting('transaction_isolation') <> 'read committed'
       OR receipt.hash_version IS DISTINCT FROM 2 OR receipt.response_status IS DISTINCT FROM 201
       OR receipt.operation_id IS DISTINCT FROM anchor
       OR receipt.response IS DISTINCT FROM public.coinpup_business_credit_response(entry.id) THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='Business credit shape is invalid',
            CONSTRAINT='ck_business_credit_shape';
    END IF;
    FOR item IN SELECT journal_id FROM public.business_credit_lines WHERE credit_id=entry.id LOOP
        PERFORM public.coinpup_validate_credit_posting_fact(item.journal_id,
            public.coinpup_validate_journal_common(item.journal_id));
    END LOOP;
END; $$;
"""


def upgrade():
    op.execute("SELECT pg_advisory_xact_lock(18945999704708432)")
    op.execute(_POSTING)
    op.execute(_ORIGIN)


def downgrade():
    op.execute("SELECT pg_advisory_xact_lock(18945999704708432)")
    op.execute(
        "LOCK TABLE public.business_credit_revisions, "
        "public.business_credit_revision_members IN ACCESS EXCLUSIVE MODE"
    )
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM public.business_credit_revisions)
               OR EXISTS (SELECT 1 FROM public.business_credit_revision_members)
               OR EXISTS (SELECT 1 FROM public.change_log WHERE entity_type IN
                   ('business_credit_revision_members','business_credit_revisions')
                   OR (entity_type='business_credits' AND entity_version>1)) THEN
                RAISE EXCEPTION USING ERRCODE='23514',
                    MESSAGE='Credit revision history prevents downgrade',
                    CONSTRAINT='ck_credit_revision_downgrade';
            END IF;
        END; $$;
    """)
    op.execute("DROP FUNCTION coinpup_validate_credit_origin_facts(uuid)")
    op.execute("DROP FUNCTION coinpup_validate_credit_posting_fact(uuid,jsonb)")
