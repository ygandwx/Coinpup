"""Use one canonical current credit projection while revision writes remain explicitly gated."""

from alembic import op

revision = "20261106_0046"
down_revision = "20261106_0045"
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
        "          + (SELECT coalesce(sum(f.payment_amount),0) FROM public.business_refunds f "
        "WHERE f.payment_id=payment.id AND f.ledger_id=allocation.ledger_id) "
        "> payment.amount THEN\n"
    )
    + r"""        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='Allocation shape is invalid',
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
                   AND a.ledger_id=allocation.ledger_id)
              + (SELECT coalesce(sum(c.total_amount),0) FROM public.business_credit_lines c
                 WHERE c.source_line_id=item.document_line_id
                   AND c.ledger_id=allocation.ledger_id) > item.total_amount
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

_OLD_CREDIT = r"""
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

"""

_ALLOCATION_SOURCE = (
    "FROM public.business_credit_lines c\n"
    "                 WHERE c.source_line_id=item.document_line_id\n"
    "                   AND c.ledger_id=allocation.ledger_id"
)
_ALLOCATION_CURRENT = (
    "FROM public.coinpup_current_credit_lines(\n"
    "                 allocation.ledger_id,ARRAY[item.document_line_id]) c"
)
_CREDIT_SOURCE = (
    "FROM public.business_credit_lines c\n"
    "               WHERE c.source_line_id=item.source_line_id AND c.ledger_id=entry.ledger_id"
)
_CREDIT_CURRENT = (
    "FROM public.coinpup_current_credit_lines(\n"
    "               entry.ledger_id,ARRAY[item.source_line_id]) c"
)
_ALLOCATION = _OLD_ALLOCATION.replace(_ALLOCATION_SOURCE, _ALLOCATION_CURRENT)
_CREDIT = _OLD_CREDIT.replace(_CREDIT_SOURCE, _CREDIT_CURRENT)


def upgrade():
    op.execute("SELECT pg_advisory_xact_lock(18945999704708432)")
    op.execute(_ALLOCATION)
    op.execute(_CREDIT)


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
    op.execute(_OLD_CREDIT)
    op.execute(_OLD_ALLOCATION)
