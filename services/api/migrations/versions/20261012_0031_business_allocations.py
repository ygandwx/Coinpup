"""Activate exact, capacity-limited allocation groups with fresh locked snapshots."""

from alembic import op

revision = "20261012_0031"
down_revision = "20261012_0030"
branch_labels = None
depends_on = None

_OLD_KINDS = (
    "kind IN ('opening', 'income', 'expense', 'transfer', 'exchange', "
    "'invoice_accrual', 'bill_accrual', 'payment')"
)
_KINDS = _OLD_KINDS[:-1] + ", 'allocation')"

_OLD_DISPATCH = r"""
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
            ELSE
                RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'Unknown operation kind',
                    CONSTRAINT = 'ck_journal_shape';
        END CASE;
    END;
    $$
"""

_OLD_GUARD = r"""
CREATE OR REPLACE FUNCTION coinpup_guard_business_settlement() RETURNS trigger
LANGUAGE plpgsql SET search_path=pg_catalog AS $$
BEGIN
    IF TG_OP <> 'INSERT' THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='Business settlement history is immutable',
            CONSTRAINT='ck_business_settlement_immutable';
    END IF;
    PERFORM pg_advisory_xact_lock(18945999704708432);
    RETURN NEW;
END; $$;
"""

_OLD_GATE = r"""
CREATE OR REPLACE FUNCTION coinpup_validate_business_allocation(allocation_identifier uuid)
RETURNS void
LANGUAGE plpgsql SET search_path=pg_catalog AS $$
DECLARE members integer; invalid boolean;
BEGIN
    SELECT count(*), coalesce(bool_or(f.kind IS DISTINCT FROM 'allocation'), false)
      INTO members, invalid FROM public.business_allocation_lines l
      LEFT JOIN public.financial_operations f ON f.id=l.operation_id AND f.ledger_id=l.ledger_id
      WHERE l.allocation_id=allocation_identifier;
    IF NOT EXISTS (SELECT 1 FROM public.business_allocations WHERE id=allocation_identifier)
       OR members NOT BETWEEN 1 AND 200 OR invalid THEN
        RAISE EXCEPTION USING ERRCODE='23514',
            MESSAGE='Allocation requires complete dedicated financial operations',
            CONSTRAINT='ck_business_allocation_shape';
    END IF;
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
               CASE payment.direction WHEN 'receipt' THEN 'invoice' ELSE 'bill' END
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
               <> CASE WHEN same_asset THEN 2 ELSE 4 END
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

CREATE FUNCTION coinpup_validate_shape_allocation(journal_identifier uuid, common_result jsonb)
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

_GUARD = _OLD_GUARD.replace(
    "    PERFORM pg_advisory_xact_lock",
    "    IF TG_TABLE_NAME IN ('business_allocations','business_allocation_lines')\n"
    "       AND current_setting('transaction_isolation') <> 'read committed' THEN\n"
    "        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='Allocation requires read committed',\n"
    "            CONSTRAINT='ck_business_allocation_isolation';\n"
    "    END IF;\n    PERFORM pg_advisory_xact_lock",
)
_DISPATCH = _OLD_DISPATCH.replace(
    "            ELSE\n",
    "            WHEN 'allocation' THEN\n"
    "                PERFORM public.coinpup_validate_shape_allocation(\n"
    "                    journal_identifier, common_result);\n"
    "            ELSE\n",
)


def _kinds(value):
    op.drop_constraint("ck_financial_operations_kind", "financial_operations", type_="check")
    op.create_check_constraint("ck_financial_operations_kind", "financial_operations", value)


def upgrade():
    op.execute("SELECT pg_advisory_xact_lock(18945999704708432)")
    for definition in (_GUARD, _ALLOCATION, _DISPATCH):
        op.execute(definition)
    _kinds(_KINDS)


def downgrade():
    op.execute("SELECT pg_advisory_xact_lock(18945999704708432)")
    op.execute(
        "LOCK TABLE business_allocations, business_allocation_lines, financial_operations "
        "IN ACCESS EXCLUSIVE MODE"
    )
    op.execute("""DO $$ BEGIN
        IF EXISTS (SELECT 1 FROM public.business_allocations)
           OR EXISTS (SELECT 1 FROM public.business_allocation_lines)
           OR EXISTS (SELECT 1 FROM public.financial_operations WHERE kind='allocation')
           OR EXISTS (SELECT 1 FROM public.change_log WHERE entity_type IN
               ('business_allocations','business_allocation_lines')) THEN
            RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='Allocation history prevents downgrade',
                CONSTRAINT='ck_business_allocation_downgrade';
        END IF;
    END $$""")
    _kinds(_OLD_KINDS)
    op.execute(_OLD_DISPATCH)
    op.execute(_OLD_GATE)
    op.execute(_OLD_GUARD)
    op.execute("DROP FUNCTION coinpup_validate_shape_allocation(uuid,jsonb)")
