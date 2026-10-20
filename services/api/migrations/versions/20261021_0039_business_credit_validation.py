"""Validate whole immutable credit groups while preserving all earlier posting shapes."""

from alembic import op

revision = "20261021_0039"
down_revision = "20261021_0038"
branch_labels = None
depends_on = None

_OLD_KINDS = (
    "kind IN ('opening', 'income', 'expense', 'transfer', 'exchange', "
    "'invoice_accrual', 'bill_accrual', 'payment', 'allocation', 'unallocation', 'refund')"
)
_KINDS = _OLD_KINDS[:-1] + ", 'invoice_credit', 'bill_credit')"

_RESPONSE = r"""
CREATE FUNCTION coinpup_business_credit_response(credit_identifier uuid)
RETURNS jsonb LANGUAGE sql STABLE SET search_path=pg_catalog AS $$
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
$$;
"""

_VALIDATION = r"""
CREATE OR REPLACE FUNCTION coinpup_validate_business_credit(credit_identifier uuid)
RETURNS void LANGUAGE plpgsql SET search_path=pg_catalog AS $$
DECLARE entry public.business_credits%ROWTYPE; document public.business_documents%ROWTYPE;
    receipt public.command_receipts%ROWTYPE; member_count integer; anchor uuid; item record;
    category_sign integer; category_role text; control_key text; operation_kind text;
    precision_digits integer;
BEGIN
    SELECT * INTO entry FROM public.business_credits WHERE id=credit_identifier;
    SELECT * INTO document FROM public.business_documents WHERE id=entry.document_id;
    SELECT * INTO receipt FROM public.command_receipts
        WHERE ledger_id=entry.ledger_id AND key=entry.idempotency_key;
    SELECT count(*) INTO member_count FROM public.business_credit_lines WHERE credit_id=entry.id;
    SELECT operation_id INTO anchor FROM public.business_credit_lines
        WHERE credit_id=entry.id AND line_no=1;
    SELECT scale INTO precision_digits FROM public.assets WHERE asset_id=document.asset_id;
    IF entry.id IS NULL OR document.id IS NULL
       OR document.ledger_id IS DISTINCT FROM entry.ledger_id
       OR document.state IS DISTINCT FROM 'confirmed'
       OR document.document_kind NOT IN ('invoice','bill')
       OR entry.source_version IS DISTINCT FROM document.version
       OR entry.credit_date < document.issue_date OR precision_digits IS NULL
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
    category_sign := CASE document.document_kind WHEN 'invoice' THEN 1 ELSE -1 END;
    category_role := CASE document.document_kind WHEN 'invoice' THEN 'income' ELSE 'expense' END;
    control_key := CASE document.document_kind WHEN 'invoice' THEN 'receivable.customer'
        ELSE 'payable.supplier' END;
    operation_kind := CASE document.document_kind WHEN 'invoice' THEN 'invoice_credit'
        ELSE 'bill_credit' END;
    FOR item IN SELECT l.*, s.id AS source_id, s.net_amount AS source_net,
        s.tax_amount AS source_tax, s.total_amount AS source_total, s.category_id, s.project_id,
        p.operation_id AS posted_operation, p.journal_id AS posted_journal,
        o.ledger_id AS original_ledger, o.kind AS original_kind, o.version AS original_version,
        o.status AS original_status, o.current_journal_id AS original_journal,
        n.ledger_id AS operation_ledger, n.kind, n.version AS operation_version,
        n.status, n.current_journal_id, n.created_by,
        j.ledger_id AS journal_ledger, j.operation_id AS journal_operation,
        j.operation_version AS journal_version, j.journal_kind, j.reverses_journal_id,
        j.transaction_date, j.recognition_date AS journal_recognition,
        j.description, j.revision_reason
        FROM public.business_credit_lines l
        LEFT JOIN public.business_document_lines s
          ON s.id=l.source_line_id AND s.document_id=l.document_id AND s.ledger_id=l.ledger_id
        LEFT JOIN public.business_postings p
          ON p.line_id=s.id AND p.document_id=s.document_id AND p.ledger_id=s.ledger_id
        LEFT JOIN public.financial_operations o ON o.id=l.source_operation_id
        LEFT JOIN public.financial_operations n ON n.id=l.operation_id
        LEFT JOIN public.journals j ON j.id=l.journal_id WHERE l.credit_id=entry.id LOOP
        IF item.source_id IS NULL OR item.posted_operation IS NULL
           OR item.ledger_id IS DISTINCT FROM entry.ledger_id
           OR item.document_id IS DISTINCT FROM entry.document_id
           OR item.source_operation_id IS DISTINCT FROM item.posted_operation
           OR item.original_ledger IS DISTINCT FROM entry.ledger_id
           OR item.original_kind IS DISTINCT FROM
               (CASE document.document_kind WHEN 'invoice' THEN 'invoice_accrual'
                   ELSE 'bill_accrual' END)
           OR item.original_version IS DISTINCT FROM 1
           OR item.original_status IS DISTINCT FROM 'active'
           OR item.original_journal IS DISTINCT FROM item.posted_journal
           OR item.kind IS DISTINCT FROM operation_kind
           OR item.operation_ledger IS DISTINCT FROM entry.ledger_id
           OR item.operation_version IS DISTINCT FROM 1 OR item.status IS DISTINCT FROM 'active'
           OR item.current_journal_id IS DISTINCT FROM item.journal_id
           OR item.created_by IS DISTINCT FROM entry.created_by
           OR item.journal_ledger IS DISTINCT FROM entry.ledger_id
           OR item.journal_operation IS DISTINCT FROM item.operation_id
           OR item.journal_version IS DISTINCT FROM 1
           OR item.journal_kind IS DISTINCT FROM 'posting' OR item.reverses_journal_id IS NOT NULL
           OR item.transaction_date IS DISTINCT FROM entry.credit_date
           OR item.journal_recognition IS DISTINCT FROM item.recognition_date
           OR item.description IS DISTINCT FROM entry.reason OR item.revision_reason IS NOT NULL
           OR item.source_total<=0
           OR trunc(item.net_amount,precision_digits)<>item.net_amount
           OR trunc(item.tax_amount,precision_digits)<>item.tax_amount
           OR (SELECT sum(c.net_amount) FROM public.business_credit_lines c
               WHERE c.source_line_id=item.source_line_id AND c.ledger_id=entry.ledger_id)
              >item.source_net
           OR (SELECT sum(c.tax_amount) FROM public.business_credit_lines c
               WHERE c.source_line_id=item.source_line_id AND c.ledger_id=entry.ledger_id)
              >item.source_tax
           OR (SELECT sum(c.total_amount) FROM public.business_credit_lines c
               WHERE c.source_line_id=item.source_line_id AND c.ledger_id=entry.ledger_id)
              + (SELECT coalesce(sum(a.document_amount),0) FROM public.business_allocation_lines a
                 WHERE a.document_line_id=item.source_line_id AND a.ledger_id=entry.ledger_id)
              - (SELECT coalesce(sum(r.document_amount),0)
                 FROM public.business_allocation_return_lines r
                 JOIN public.business_allocation_lines a
                   ON a.id=r.source_allocation_line_id AND a.ledger_id=r.ledger_id
                 WHERE a.document_line_id=item.source_line_id AND a.ledger_id=entry.ledger_id)
              >item.source_total
           OR EXISTS (SELECT 1 FROM public.opening_positions WHERE operation_id=item.operation_id)
           OR (SELECT count(*) FROM public.journal_lines WHERE journal_id=item.journal_id)<>2
           OR EXISTS (SELECT 1 FROM public.journal_lines l WHERE l.journal_id=item.journal_id AND (
               l.component_no<>0 OR l.asset_id IS DISTINCT FROM document.asset_id
               OR l.party_id IS DISTINCT FROM document.party_id
               OR l.document_id IS DISTINCT FROM document.id
               OR l.document_line_id IS DISTINCT FROM item.source_line_id
               OR l.project_id IS DISTINCT FROM item.project_id
               OR l.counterparty_entity_id IS NOT NULL OR l.dimension_owner_id IS NOT NULL))
           OR (SELECT count(*) FROM public.journal_lines l
               JOIN public.accounts a ON a.id=l.account_id AND a.ledger_id=l.ledger_id
               WHERE l.journal_id=item.journal_id AND l.role='account'
                   AND a.system_key=control_key AND l.amount=-category_sign*item.total_amount)<>1
           OR (SELECT count(*) FROM public.journal_lines l WHERE l.journal_id=item.journal_id
               AND l.role=category_role AND l.category_id=item.category_id
               AND l.amount=category_sign*item.total_amount)<>1 THEN
            RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='Business credit line shape is invalid',
                CONSTRAINT='ck_business_credit_shape';
        END IF;
    END LOOP;
END; $$;

CREATE FUNCTION coinpup_validate_shape_credit(journal_identifier uuid, common_result jsonb)
RETURNS void LANGUAGE plpgsql SET search_path=pg_catalog AS $$
BEGIN
    PERFORM public.coinpup_validate_business_credit(
        (SELECT credit_id FROM public.business_credit_lines WHERE journal_id=journal_identifier));
    IF common_result->>'error_message' IS NOT NULL THEN
        RAISE EXCEPTION USING ERRCODE=common_result->>'error_sqlstate',
            MESSAGE=common_result->>'error_message', CONSTRAINT=common_result->>'error_constraint';
    END IF;
END; $$;
"""

_OLD_DISPATCH = r"""
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
        WHEN 'refund' THEN
            PERFORM public.coinpup_validate_shape_refund(journal_identifier, common_result);
        ELSE RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='Unknown operation kind',
            CONSTRAINT='ck_journal_shape';
    END CASE;
END; $$;
"""

_DISPATCH = _OLD_DISPATCH.replace(
    "        ELSE RAISE EXCEPTION",
    "        WHEN 'invoice_credit', 'bill_credit' THEN\n"
    "            PERFORM public.coinpup_validate_shape_credit("
    "journal_identifier, common_result);\n"
    "        ELSE RAISE EXCEPTION",
)


def _kinds(value):
    op.drop_constraint("ck_financial_operations_kind", "financial_operations", type_="check")
    op.create_check_constraint("ck_financial_operations_kind", "financial_operations", value)


def upgrade():
    op.execute("SELECT pg_advisory_xact_lock(18945999704708432)")
    _kinds(_KINDS)
    for sql in (_RESPONSE, _VALIDATION, _DISPATCH):
        op.execute(sql)


def downgrade():
    op.execute("SELECT pg_advisory_xact_lock(18945999704708432)")
    op.execute("LOCK TABLE business_credits, business_credit_lines IN ACCESS EXCLUSIVE MODE")
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM public.business_credits)
               OR EXISTS (SELECT 1 FROM public.business_credit_lines)
               OR EXISTS (SELECT 1 FROM public.financial_operations
                   WHERE kind IN ('invoice_credit','bill_credit'))
               OR EXISTS (SELECT 1 FROM public.change_log WHERE entity_type IN
                   ('business_credits','business_credit_lines')) THEN
                RAISE EXCEPTION USING ERRCODE='23514',
                    MESSAGE='Business credit history prevents downgrade',
                    CONSTRAINT='ck_business_credit_downgrade';
            END IF;
        END; $$;
    """)
    op.execute(_OLD_DISPATCH)
    op.execute("""
        CREATE OR REPLACE FUNCTION coinpup_validate_business_credit(credit_identifier uuid)
        RETURNS void LANGUAGE plpgsql SET search_path=pg_catalog AS $$
        BEGIN
            RAISE EXCEPTION USING ERRCODE='23514',
                MESSAGE='Business credit requires dedicated validation',
                CONSTRAINT='ck_business_credit_shape';
        END; $$;
    """)
    op.execute("DROP FUNCTION coinpup_validate_shape_credit(uuid,jsonb)")
    op.execute("DROP FUNCTION coinpup_business_credit_response(uuid)")
    _kinds(_OLD_KINDS)
