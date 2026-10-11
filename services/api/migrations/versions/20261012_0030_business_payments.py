"""Activate exact payment postings; allocation activation remains separately gated."""

from alembic import op

revision = "20261012_0030"
down_revision = "20261012_0029"
branch_labels = None
depends_on = None

_OLD_KINDS = (
    "kind IN ('opening', 'income', 'expense', 'transfer', 'exchange', 'invoice_accrual', "
    "'bill_accrual')"
)
_KINDS = _OLD_KINDS[:-1] + ", 'payment')"

_OLD_GATE = r"""
CREATE OR REPLACE FUNCTION coinpup_validate_business_payment(payment_identifier uuid) RETURNS void
LANGUAGE plpgsql SET search_path=pg_catalog AS $$
DECLARE operation_kind text;
BEGIN
    SELECT f.kind INTO operation_kind FROM public.business_payments p
      LEFT JOIN public.financial_operations f ON f.id=p.operation_id AND f.ledger_id=p.ledger_id
      WHERE p.id=payment_identifier;
    IF operation_kind IS DISTINCT FROM 'payment' THEN
        RAISE EXCEPTION USING ERRCODE='23514',
            MESSAGE='Payment requires its dedicated financial operation',
            CONSTRAINT='ck_business_payment_shape';
    END IF;
END; $$;


"""

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
            ELSE
                RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'Unknown operation kind',
                    CONSTRAINT = 'ck_journal_shape';
        END CASE;
    END;
    $$


"""

_RESPONSES = r"""
CREATE FUNCTION coinpup_business_allocation_response(allocation_identifier uuid)
RETURNS jsonb LANGUAGE sql STABLE SET search_path=pg_catalog AS $$
    SELECT jsonb_build_object(
        'kind','business_allocation','id',a.id,'ledger_id',a.ledger_id,
        'payment_id',a.payment_id,'postings',coalesce((
            SELECT jsonb_agg(jsonb_build_object(
                'line_no',l.line_no,'document_id',l.document_id,
                'document_line_id',l.document_line_id,'operation_id',l.operation_id,
                'journal_id',l.journal_id) ORDER BY l.line_no)
            FROM public.business_allocation_lines l
            WHERE l.allocation_id=a.id AND l.ledger_id=a.ledger_id
        ),'[]'::jsonb))
    FROM public.business_allocations a WHERE a.id=allocation_identifier;
$$;

CREATE FUNCTION coinpup_business_payment_response(payment_identifier uuid)
RETURNS jsonb LANGUAGE sql STABLE SET search_path=pg_catalog AS $$
    SELECT jsonb_build_object(
        'kind','business_payment','id',p.id,'ledger_id',p.ledger_id,
        'operation_id',p.operation_id,'journal_id',p.journal_id,
        'initial_allocation',public.coinpup_business_allocation_response(p.initial_allocation_id))
    FROM public.business_payments p WHERE p.id=payment_identifier;
$$;

"""

_PAYMENT = r"""
CREATE OR REPLACE FUNCTION coinpup_validate_business_payment(payment_identifier uuid)
RETURNS void LANGUAGE plpgsql SET search_path=pg_catalog AS $$
DECLARE payment public.business_payments%ROWTYPE; header public.journals%ROWTYPE;
        operation public.financial_operations%ROWTYPE; receipt public.command_receipts%ROWTYPE;
        bank_sign integer; control_key text; fee jsonb; fee_component integer;
        precision_digits integer; fee_amount numeric;
BEGIN
    SELECT * INTO payment FROM public.business_payments WHERE id=payment_identifier;
    SELECT * INTO header FROM public.journals WHERE id=payment.journal_id;
    SELECT * INTO operation FROM public.financial_operations WHERE id=payment.operation_id;
    SELECT * INTO receipt FROM public.command_receipts
        WHERE ledger_id=payment.ledger_id AND key=payment.idempotency_key;
    bank_sign := CASE payment.direction WHEN 'receipt' THEN 1 ELSE -1 END;
    control_key := CASE payment.direction WHEN 'receipt' THEN 'advance.received'
        ELSE 'advance.paid' END;
    IF payment.id IS NULL OR header.id IS NULL OR operation.id IS NULL
       OR operation.kind IS DISTINCT FROM 'payment'
       OR operation.ledger_id IS DISTINCT FROM payment.ledger_id
       OR operation.version IS DISTINCT FROM 1 OR operation.status IS DISTINCT FROM 'active'
       OR operation.current_journal_id IS DISTINCT FROM header.id
       OR operation.created_by IS DISTINCT FROM payment.created_by
       OR header.operation_id IS DISTINCT FROM payment.operation_id
       OR header.ledger_id IS DISTINCT FROM payment.ledger_id
       OR header.operation_version IS DISTINCT FROM 1
       OR header.journal_kind IS DISTINCT FROM 'posting' OR header.reverses_journal_id IS NOT NULL
       OR header.transaction_date IS DISTINCT FROM payment.transaction_date
       OR header.recognition_date IS DISTINCT FROM payment.transaction_date
       OR header.description IS DISTINCT FROM payment.description
       OR header.revision_reason IS NOT NULL
       OR EXISTS (SELECT 1 FROM public.opening_positions WHERE operation_id=operation.id)
       OR receipt.hash_version IS DISTINCT FROM 2 OR receipt.response_status IS DISTINCT FROM 201
       OR receipt.operation_id IS DISTINCT FROM operation.id
       OR receipt.response IS DISTINCT FROM public.coinpup_business_payment_response(payment.id)
       OR (payment.initial_allocation_id IS NOT NULL AND NOT EXISTS (
           SELECT 1 FROM public.business_allocations a WHERE a.id=payment.initial_allocation_id
               AND a.payment_id=payment.id AND a.ledger_id=payment.ledger_id
               AND a.created_by=payment.created_by AND a.idempotency_key=payment.idempotency_key))
       OR (SELECT count(*) FROM public.journal_lines WHERE journal_id=header.id)
           <> 2+2*jsonb_array_length(payment.fees)
       OR (SELECT count(*) FROM public.journal_lines
           WHERE journal_id=header.id AND component_no=0) <> 2
       OR EXISTS (SELECT 1 FROM public.journal_lines l WHERE l.journal_id=header.id
           AND l.component_no=0 AND (l.asset_id IS DISTINCT FROM payment.asset_id
               OR l.party_id IS DISTINCT FROM payment.party_id OR l.document_id IS NOT NULL
               OR l.document_line_id IS NOT NULL OR l.project_id IS NOT NULL
               OR l.counterparty_entity_id IS NOT NULL OR l.dimension_owner_id IS NOT NULL))
       OR (SELECT count(*) FROM public.journal_lines l JOIN public.accounts a ON a.id=l.account_id
           WHERE l.journal_id=header.id AND l.component_no=0 AND l.role='account'
               AND l.account_id=payment.account_id AND a.account_class='money'
               AND l.amount=bank_sign*payment.amount) <> 1
       OR (SELECT count(*) FROM public.journal_lines l JOIN public.accounts a ON a.id=l.account_id
           WHERE l.journal_id=header.id AND l.component_no=0 AND l.role='account'
               AND a.system_key=control_key AND l.amount=-bank_sign*payment.amount) <> 1 THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='Payment shape is invalid',
            CONSTRAINT='ck_business_payment_shape';
    END IF;
    FOR fee, fee_component IN SELECT value, ordinality FROM jsonb_array_elements(payment.fees)
        WITH ORDINALITY LOOP
        IF jsonb_typeof(fee) IS DISTINCT FROM 'object' THEN
            RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='Payment fee shape is invalid',
                CONSTRAINT='ck_business_payment_shape';
        END IF;
        IF fee-ARRAY['account_id','asset_id','amount','category_id'] <> '{}'::jsonb
           OR jsonb_typeof(fee->'account_id') IS DISTINCT FROM 'string'
           OR jsonb_typeof(fee->'asset_id') IS DISTINCT FROM 'string'
           OR jsonb_typeof(fee->'amount') IS DISTINCT FROM 'string'
           OR jsonb_typeof(fee->'category_id') IS DISTINCT FROM 'string' THEN
            RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='Payment fee shape is invalid',
                CONSTRAINT='ck_business_payment_shape';
        END IF;
        SELECT scale INTO precision_digits FROM public.assets WHERE asset_id=fee->>'asset_id';
        IF precision_digits IS NULL OR fee->>'amount' !~ '^(0|[1-9][0-9]{0,19})(\.[0-9]{1,18})?$'
           OR length(split_part(fee->>'amount','.',2)) > precision_digits THEN
            RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='Payment fee quantity is invalid',
                CONSTRAINT='ck_business_payment_shape';
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
            RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='Payment fee shape is invalid',
                CONSTRAINT='ck_business_payment_shape';
        END IF;
    END LOOP;
END; $$;

CREATE FUNCTION coinpup_validate_shape_payment(journal_identifier uuid, common_result jsonb)
RETURNS void LANGUAGE plpgsql SET search_path=pg_catalog AS $$
BEGIN
    PERFORM public.coinpup_validate_business_payment(
        (SELECT id FROM public.business_payments WHERE journal_id=journal_identifier));
    IF common_result->>'error_message' IS NOT NULL THEN
        RAISE EXCEPTION USING ERRCODE=common_result->>'error_sqlstate',
            MESSAGE=common_result->>'error_message', CONSTRAINT=common_result->>'error_constraint';
    END IF;
END; $$;

"""

_DISPATCH = _OLD_DISPATCH.replace(
    "            ELSE\n",
    "            WHEN 'payment' THEN\n"
    "                PERFORM public.coinpup_validate_shape_payment(\n"
    "                    journal_identifier, common_result);\n"
    "            ELSE\n",
)


def _kinds(value):
    op.drop_constraint("ck_financial_operations_kind", "financial_operations", type_="check")
    op.create_check_constraint("ck_financial_operations_kind", "financial_operations", value)


def upgrade():
    op.execute("SELECT pg_advisory_xact_lock(18945999704708432)")
    for definition in (_RESPONSES, _PAYMENT, _DISPATCH):
        op.execute(definition)
    _kinds(_KINDS)


def downgrade():
    op.execute("SELECT pg_advisory_xact_lock(18945999704708432)")
    op.execute("LOCK TABLE business_payments, financial_operations IN ACCESS EXCLUSIVE MODE")
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM public.business_payments)
               OR EXISTS (SELECT 1 FROM public.financial_operations WHERE kind='payment')
               OR EXISTS (SELECT 1 FROM public.change_log
                   WHERE entity_type='business_payments') THEN
                RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='Payment history prevents downgrade',
                    CONSTRAINT='ck_business_payment_downgrade';
            END IF;
        END; $$;
    """)
    _kinds(_OLD_KINDS)
    op.execute(_OLD_DISPATCH)
    op.execute(_OLD_GATE)
    for function in (
        "coinpup_validate_shape_payment(uuid,jsonb)",
        "coinpup_business_payment_response(uuid)",
        "coinpup_business_allocation_response(uuid)",
    ):
        op.execute("DROP FUNCTION " + function)
