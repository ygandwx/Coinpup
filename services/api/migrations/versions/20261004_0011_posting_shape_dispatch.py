"""Dispatch posting shape checks without changing revision validation or error priority."""

from alembic import op

revision = "20261004_0011"
down_revision = "20261004_0010"
branch_labels = None
depends_on = None

# These SQL definitions are frozen in this migration. Common checks report their
# first error; each kind raises it only after its original principal/date checks.

_COMMON_SQL = """
    CREATE FUNCTION coinpup_validate_journal_common(journal_identifier uuid)
    RETURNS jsonb LANGUAGE plpgsql SET search_path = pg_catalog AS $$
    DECLARE operation_kind text;
            line_count integer; account_count integer; asset_count integer;
            fee_count integer; maximum_component integer;
            error_sqlstate text; error_message text; error_constraint text;
    BEGIN
        SELECT operation.kind INTO operation_kind
        FROM public.journals header
        JOIN public.financial_operations operation ON operation.id = header.operation_id
        WHERE header.id = journal_identifier;
        SELECT count(*), count(*) FILTER (WHERE role = 'account'), count(DISTINCT asset_id)
            INTO line_count, account_count, asset_count FROM public.journal_lines
            WHERE journal_id = journal_identifier AND component_no = 0;
        SELECT count(DISTINCT component_no), coalesce(max(component_no), 0)
            INTO fee_count, maximum_component FROM public.journal_lines
            WHERE journal_id = journal_identifier AND component_no > 0;
        -- Defer these errors until after the kind-specific principal checks.
        -- The first error has the same priority as in the original validator.
        IF maximum_component <> fee_count OR (operation_kind = 'opening' AND fee_count <> 0) THEN
            error_sqlstate := '23514';
            error_message := 'Fee components must be contiguous and cannot belong to an opening';
            error_constraint := 'ck_journal_fees';
        ELSIF EXISTS (
            SELECT 1 FROM public.journal_lines
            WHERE journal_id = journal_identifier AND component_no > 0
            GROUP BY component_no HAVING count(*) <> 2 OR count(DISTINCT asset_id) <> 1 OR
                count(*) FILTER (WHERE role = 'account' AND amount < 0) <> 1 OR
                count(*) FILTER (WHERE role = 'expense' AND amount > 0) <> 1 OR sum(amount) <> 0
        ) THEN
            error_sqlstate := '23514';
            error_message := 'Each fee must balance one account outflow against one expense';
            error_constraint := 'ck_journal_fees';
        ELSIF EXISTS (
            SELECT 1 FROM public.journal_lines WHERE journal_id = journal_identifier
            GROUP BY asset_id HAVING sum(amount) <> 0
        ) THEN
            error_sqlstate := '23514';
            error_message := 'Journal does not balance by asset';
            error_constraint := 'ck_journal_balanced';
        END IF;
        RETURN jsonb_build_object(
            'line_count', line_count, 'account_count', account_count, 'asset_count', asset_count,
            'error_sqlstate', error_sqlstate, 'error_message', error_message,
            'error_constraint', error_constraint
        );
    END;
    $$
"""

_OPENING_SQL = """
    CREATE FUNCTION coinpup_validate_shape_opening(
        journal_identifier uuid, common_result jsonb
    ) RETURNS void LANGUAGE plpgsql SET search_path = pg_catalog AS $$
    DECLARE header public.journals%ROWTYPE; operation public.financial_operations%ROWTYPE;
            line_count integer; account_count integer; asset_count integer;
    BEGIN
        SELECT * INTO header FROM public.journals WHERE id = journal_identifier;
        SELECT * INTO operation FROM public.financial_operations WHERE id = header.operation_id;
        line_count := (common_result->>'line_count')::integer;
        account_count := (common_result->>'account_count')::integer;
        asset_count := (common_result->>'asset_count')::integer;
        IF line_count < 2 OR account_count <> 1 OR asset_count <> 1 THEN
            RAISE EXCEPTION USING ERRCODE = '23514',
                MESSAGE = 'Initial journal shape is invalid', CONSTRAINT = 'ck_journal_shape';
        END IF;
        IF common_result->>'error_message' IS NOT NULL THEN
            RAISE EXCEPTION USING ERRCODE = common_result->>'error_sqlstate',
                MESSAGE = common_result->>'error_message',
                CONSTRAINT = common_result->>'error_constraint';
        END IF;
        IF line_count <> 2 OR header.recognition_date <> header.transaction_date OR
           (SELECT count(*) FROM public.journal_lines WHERE journal_id = journal_identifier
                AND component_no = 0 AND role = 'equity') <> 1 THEN
            RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'Opening shape is invalid',
                CONSTRAINT = 'ck_journal_shape';
        END IF;
        IF NOT EXISTS (
            SELECT 1 FROM public.opening_positions opening
            JOIN public.journal_lines line ON line.journal_id = journal_identifier
                AND line.component_no = 0 AND line.role = 'account'
                AND line.account_id = opening.account_id AND line.asset_id = opening.asset_id
                AND line.ledger_id = opening.ledger_id
            WHERE opening.operation_id = operation.id AND opening.ledger_id = header.ledger_id
        ) THEN
            RAISE EXCEPTION USING ERRCODE = '23514',
                MESSAGE = 'Opening position is inconsistent',
                CONSTRAINT = 'ck_opening_position';
        END IF;
    END;
    $$
"""

_INCOME_SQL = """
    CREATE FUNCTION coinpup_validate_shape_income(
        journal_identifier uuid, common_result jsonb
    ) RETURNS void LANGUAGE plpgsql SET search_path = pg_catalog AS $$
    DECLARE header public.journals%ROWTYPE; operation public.financial_operations%ROWTYPE;
            line_count integer; account_count integer; asset_count integer;
    BEGIN
        SELECT * INTO header FROM public.journals WHERE id = journal_identifier;
        SELECT * INTO operation FROM public.financial_operations WHERE id = header.operation_id;
        line_count := (common_result->>'line_count')::integer;
        account_count := (common_result->>'account_count')::integer;
        asset_count := (common_result->>'asset_count')::integer;
        IF line_count < 2 OR account_count <> 1 OR asset_count <> 1 THEN
            RAISE EXCEPTION USING ERRCODE = '23514',
                MESSAGE = 'Initial journal shape is invalid', CONSTRAINT = 'ck_journal_shape';
        END IF;
        IF common_result->>'error_message' IS NOT NULL THEN
            RAISE EXCEPTION USING ERRCODE = common_result->>'error_sqlstate',
                MESSAGE = common_result->>'error_message',
                CONSTRAINT = common_result->>'error_constraint';
        END IF;
        IF EXISTS (
            SELECT 1 FROM public.opening_positions WHERE operation_id = operation.id
        ) THEN
            RAISE EXCEPTION USING ERRCODE = '23514',
                MESSAGE = 'Only openings have an opening position',
                CONSTRAINT = 'ck_opening_position';
        END IF;
        IF operation.kind = 'income' AND EXISTS (
            SELECT 1 FROM public.journal_lines WHERE journal_id = journal_identifier
                AND component_no = 0 AND ((role = 'account' AND amount <= 0) OR
                    (role <> 'account' AND (role <> 'income' OR amount >= 0)))
        ) THEN
            RAISE EXCEPTION USING ERRCODE = '23514',
                MESSAGE = 'Income signs or roles are invalid', CONSTRAINT = 'ck_journal_shape';
        END IF;
    END;
    $$
"""

_EXPENSE_SQL = """
    CREATE FUNCTION coinpup_validate_shape_expense(
        journal_identifier uuid, common_result jsonb
    ) RETURNS void LANGUAGE plpgsql SET search_path = pg_catalog AS $$
    DECLARE header public.journals%ROWTYPE; operation public.financial_operations%ROWTYPE;
            line_count integer; account_count integer; asset_count integer;
    BEGIN
        SELECT * INTO header FROM public.journals WHERE id = journal_identifier;
        SELECT * INTO operation FROM public.financial_operations WHERE id = header.operation_id;
        line_count := (common_result->>'line_count')::integer;
        account_count := (common_result->>'account_count')::integer;
        asset_count := (common_result->>'asset_count')::integer;
        IF line_count < 2 OR account_count <> 1 OR asset_count <> 1 THEN
            RAISE EXCEPTION USING ERRCODE = '23514',
                MESSAGE = 'Initial journal shape is invalid', CONSTRAINT = 'ck_journal_shape';
        END IF;
        IF common_result->>'error_message' IS NOT NULL THEN
            RAISE EXCEPTION USING ERRCODE = common_result->>'error_sqlstate',
                MESSAGE = common_result->>'error_message',
                CONSTRAINT = common_result->>'error_constraint';
        END IF;
        IF EXISTS (
            SELECT 1 FROM public.opening_positions WHERE operation_id = operation.id
        ) THEN
            RAISE EXCEPTION USING ERRCODE = '23514',
                MESSAGE = 'Only openings have an opening position',
                CONSTRAINT = 'ck_opening_position';
        END IF;
        IF operation.kind = 'expense' AND EXISTS (
            SELECT 1 FROM public.journal_lines WHERE journal_id = journal_identifier
                AND component_no = 0 AND ((role = 'account' AND amount >= 0) OR
                    (role <> 'account' AND (role <> 'expense' OR amount <= 0)))
        ) THEN
            RAISE EXCEPTION USING ERRCODE = '23514',
                MESSAGE = 'Expense signs or roles are invalid', CONSTRAINT = 'ck_journal_shape';
        END IF;
    END;
    $$
"""

_TRANSFER_SQL = """
    CREATE FUNCTION coinpup_validate_shape_transfer(
        journal_identifier uuid, common_result jsonb
    ) RETURNS void LANGUAGE plpgsql SET search_path = pg_catalog AS $$
    DECLARE header public.journals%ROWTYPE; operation public.financial_operations%ROWTYPE;
            line_count integer; account_count integer; asset_count integer;
    BEGIN
        SELECT * INTO header FROM public.journals WHERE id = journal_identifier;
        SELECT * INTO operation FROM public.financial_operations WHERE id = header.operation_id;
        line_count := (common_result->>'line_count')::integer;
        account_count := (common_result->>'account_count')::integer;
        asset_count := (common_result->>'asset_count')::integer;
        IF line_count <> 2 OR account_count <> 2 OR asset_count <> 1 OR
           (SELECT count(DISTINCT account_id) FROM public.journal_lines
                WHERE journal_id = journal_identifier AND component_no = 0) <> 2 THEN
            RAISE EXCEPTION USING ERRCODE = '23514',
                MESSAGE = 'Transfer requires two distinct accounts of the same asset',
                CONSTRAINT = 'ck_journal_shape';
        END IF;
        IF header.recognition_date <> header.transaction_date THEN
            RAISE EXCEPTION USING ERRCODE = '23514',
                MESSAGE = 'Transfer dates must match', CONSTRAINT = 'ck_journal_shape';
        END IF;
        IF common_result->>'error_message' IS NOT NULL THEN
            RAISE EXCEPTION USING ERRCODE = common_result->>'error_sqlstate',
                MESSAGE = common_result->>'error_message',
                CONSTRAINT = common_result->>'error_constraint';
        END IF;
        IF EXISTS (
            SELECT 1 FROM public.opening_positions WHERE operation_id = operation.id
        ) THEN
            RAISE EXCEPTION USING ERRCODE = '23514',
                MESSAGE = 'Only openings have an opening position',
                CONSTRAINT = 'ck_opening_position';
        END IF;
    END;
    $$
"""

_EXCHANGE_SQL = """
    CREATE FUNCTION coinpup_validate_shape_exchange(
        journal_identifier uuid, common_result jsonb
    ) RETURNS void LANGUAGE plpgsql SET search_path = pg_catalog AS $$
    DECLARE header public.journals%ROWTYPE; operation public.financial_operations%ROWTYPE;
            line_count integer; account_count integer; asset_count integer;
    BEGIN
        SELECT * INTO header FROM public.journals WHERE id = journal_identifier;
        SELECT * INTO operation FROM public.financial_operations WHERE id = header.operation_id;
        line_count := (common_result->>'line_count')::integer;
        account_count := (common_result->>'account_count')::integer;
        asset_count := (common_result->>'asset_count')::integer;
        IF line_count <> 4 OR account_count <> 2 OR asset_count <> 2 OR
           (SELECT count(*) FROM public.journal_lines
                WHERE journal_id = journal_identifier AND component_no = 0
                AND role = 'exchange') <> 2 OR
           (SELECT count(*) FROM public.journal_lines
                WHERE journal_id = journal_identifier AND component_no = 0
                AND role = 'account' AND amount < 0) <> 1 OR
           (SELECT count(*) FROM public.journal_lines
                WHERE journal_id = journal_identifier AND component_no = 0
                AND role = 'account' AND amount > 0) <> 1 THEN
            RAISE EXCEPTION USING ERRCODE = '23514',
                MESSAGE = 'Exchange requires opposite account flows in two assets',
                CONSTRAINT = 'ck_journal_shape';
        END IF;
        IF EXISTS (
            SELECT 1 FROM public.journal_lines
            WHERE journal_id = journal_identifier AND component_no = 0
            GROUP BY asset_id HAVING count(*) <> 2 OR
                count(*) FILTER (WHERE role = 'account') <> 1 OR
                count(*) FILTER (WHERE role = 'exchange') <> 1 OR sum(amount) <> 0
        ) THEN
            RAISE EXCEPTION USING ERRCODE = '23514',
                MESSAGE = 'Exchange principal must balance independently in each asset',
                CONSTRAINT = 'ck_journal_balanced';
        END IF;
        IF header.recognition_date <> header.transaction_date THEN
            RAISE EXCEPTION USING ERRCODE = '23514',
                MESSAGE = 'Exchange dates must match', CONSTRAINT = 'ck_journal_shape';
        END IF;
        IF common_result->>'error_message' IS NOT NULL THEN
            RAISE EXCEPTION USING ERRCODE = common_result->>'error_sqlstate',
                MESSAGE = common_result->>'error_message',
                CONSTRAINT = common_result->>'error_constraint';
        END IF;
        IF EXISTS (
            SELECT 1 FROM public.opening_positions WHERE operation_id = operation.id
        ) THEN
            RAISE EXCEPTION USING ERRCODE = '23514',
                MESSAGE = 'Only openings have an opening position',
                CONSTRAINT = 'ck_opening_position';
        END IF;
    END;
    $$
"""

_DISPATCH_SQL = """
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
            ELSE
                RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'Unknown operation kind',
                    CONSTRAINT = 'ck_journal_shape';
        END CASE;
    END;
    $$
"""

_LEGACY_POSTING_SHAPE_SQL = """
    CREATE OR REPLACE FUNCTION coinpup_validate_posting_shape(journal_identifier uuid)
    RETURNS void LANGUAGE plpgsql SET search_path = pg_catalog AS $$
    DECLARE header public.journals%ROWTYPE; operation public.financial_operations%ROWTYPE;
            line_count integer; account_count integer; asset_count integer;
            fee_count integer; maximum_component integer;
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
        SELECT count(*), count(*) FILTER (WHERE role = 'account'), count(DISTINCT asset_id)
            INTO line_count, account_count, asset_count FROM public.journal_lines
            WHERE journal_id = journal_identifier AND component_no = 0;
        IF operation.kind = 'transfer' THEN
            IF line_count <> 2 OR account_count <> 2 OR asset_count <> 1 OR
               (SELECT count(DISTINCT account_id) FROM public.journal_lines
                    WHERE journal_id = journal_identifier AND component_no = 0) <> 2 THEN
                RAISE EXCEPTION USING ERRCODE = '23514',
                    MESSAGE = 'Transfer requires two distinct accounts of the same asset',
                    CONSTRAINT = 'ck_journal_shape';
            END IF;
            IF header.recognition_date <> header.transaction_date THEN
                RAISE EXCEPTION USING ERRCODE = '23514',
                    MESSAGE = 'Transfer dates must match', CONSTRAINT = 'ck_journal_shape';
            END IF;
        ELSIF operation.kind = 'exchange' THEN
            IF line_count <> 4 OR account_count <> 2 OR asset_count <> 2 OR
               (SELECT count(*) FROM public.journal_lines
                    WHERE journal_id = journal_identifier AND component_no = 0
                    AND role = 'exchange') <> 2 OR
               (SELECT count(*) FROM public.journal_lines
                    WHERE journal_id = journal_identifier AND component_no = 0
                    AND role = 'account' AND amount < 0) <> 1 OR
               (SELECT count(*) FROM public.journal_lines
                    WHERE journal_id = journal_identifier AND component_no = 0
                    AND role = 'account' AND amount > 0) <> 1 THEN
                RAISE EXCEPTION USING ERRCODE = '23514',
                    MESSAGE = 'Exchange requires opposite account flows in two assets',
                    CONSTRAINT = 'ck_journal_shape';
            END IF;
            IF EXISTS (
                SELECT 1 FROM public.journal_lines
                WHERE journal_id = journal_identifier AND component_no = 0
                GROUP BY asset_id HAVING count(*) <> 2 OR
                    count(*) FILTER (WHERE role = 'account') <> 1 OR
                    count(*) FILTER (WHERE role = 'exchange') <> 1 OR sum(amount) <> 0
            ) THEN
                RAISE EXCEPTION USING ERRCODE = '23514',
                    MESSAGE = 'Exchange principal must balance independently in each asset',
                    CONSTRAINT = 'ck_journal_balanced';
            END IF;
            IF header.recognition_date <> header.transaction_date THEN
                RAISE EXCEPTION USING ERRCODE = '23514',
                    MESSAGE = 'Exchange dates must match', CONSTRAINT = 'ck_journal_shape';
            END IF;
        ELSIF line_count < 2 OR account_count <> 1 OR asset_count <> 1 THEN
            RAISE EXCEPTION USING ERRCODE = '23514',
                MESSAGE = 'Initial journal shape is invalid', CONSTRAINT = 'ck_journal_shape';
        END IF;
        SELECT count(DISTINCT component_no), coalesce(max(component_no), 0)
            INTO fee_count, maximum_component FROM public.journal_lines
            WHERE journal_id = journal_identifier AND component_no > 0;
        IF maximum_component <> fee_count OR (operation.kind = 'opening' AND fee_count <> 0) THEN
            RAISE EXCEPTION USING ERRCODE = '23514',
                MESSAGE = 'Fee components must be contiguous and cannot belong to an opening',
                CONSTRAINT = 'ck_journal_fees';
        END IF;
        IF EXISTS (
            SELECT 1 FROM public.journal_lines
            WHERE journal_id = journal_identifier AND component_no > 0
            GROUP BY component_no HAVING count(*) <> 2 OR count(DISTINCT asset_id) <> 1 OR
                count(*) FILTER (WHERE role = 'account' AND amount < 0) <> 1 OR
                count(*) FILTER (WHERE role = 'expense' AND amount > 0) <> 1 OR sum(amount) <> 0
        ) THEN
            RAISE EXCEPTION USING ERRCODE = '23514',
                MESSAGE = 'Each fee must balance one account outflow against one expense',
                CONSTRAINT = 'ck_journal_fees';
        END IF;
        IF EXISTS (
            SELECT 1 FROM public.journal_lines WHERE journal_id = journal_identifier
            GROUP BY asset_id HAVING sum(amount) <> 0
        ) THEN
            RAISE EXCEPTION USING ERRCODE = '23514',
                MESSAGE = 'Journal does not balance by asset', CONSTRAINT = 'ck_journal_balanced';
        END IF;
        IF operation.kind = 'opening' THEN
            IF line_count <> 2 OR header.recognition_date <> header.transaction_date OR
               (SELECT count(*) FROM public.journal_lines WHERE journal_id = journal_identifier
                    AND component_no = 0 AND role = 'equity') <> 1 THEN
                RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'Opening shape is invalid',
                    CONSTRAINT = 'ck_journal_shape';
            END IF;
            IF NOT EXISTS (
                SELECT 1 FROM public.opening_positions opening
                JOIN public.journal_lines line ON line.journal_id = journal_identifier
                    AND line.component_no = 0 AND line.role = 'account'
                    AND line.account_id = opening.account_id AND line.asset_id = opening.asset_id
                    AND line.ledger_id = opening.ledger_id
                WHERE opening.operation_id = operation.id AND opening.ledger_id = header.ledger_id
            ) THEN
                RAISE EXCEPTION USING ERRCODE = '23514',
                    MESSAGE = 'Opening position is inconsistent',
                    CONSTRAINT = 'ck_opening_position';
            END IF;
        ELSE
            IF EXISTS (
                SELECT 1 FROM public.opening_positions WHERE operation_id = operation.id
            ) THEN
                RAISE EXCEPTION USING ERRCODE = '23514',
                    MESSAGE = 'Only openings have an opening position',
                    CONSTRAINT = 'ck_opening_position';
            END IF;
            IF operation.kind = 'expense' AND EXISTS (
                SELECT 1 FROM public.journal_lines WHERE journal_id = journal_identifier
                    AND component_no = 0 AND ((role = 'account' AND amount >= 0) OR
                        (role <> 'account' AND (role <> 'expense' OR amount <= 0)))
            ) THEN
                RAISE EXCEPTION USING ERRCODE = '23514',
                    MESSAGE = 'Expense signs or roles are invalid', CONSTRAINT = 'ck_journal_shape';
            END IF;
            IF operation.kind = 'income' AND EXISTS (
                SELECT 1 FROM public.journal_lines WHERE journal_id = journal_identifier
                    AND component_no = 0 AND ((role = 'account' AND amount <= 0) OR
                        (role <> 'account' AND (role <> 'income' OR amount >= 0)))
            ) THEN
                RAISE EXCEPTION USING ERRCODE = '23514',
                    MESSAGE = 'Income signs or roles are invalid', CONSTRAINT = 'ck_journal_shape';
            END IF;
        END IF;

    END;
    $$
"""


def upgrade() -> None:
    op.execute(_COMMON_SQL)
    op.execute(_OPENING_SQL)
    op.execute(_INCOME_SQL)
    op.execute(_EXPENSE_SQL)
    op.execute(_TRANSFER_SQL)
    op.execute(_EXCHANGE_SQL)
    op.execute(_DISPATCH_SQL)


def downgrade() -> None:
    # Restore the exact 0007 body before removing its now-unused helpers.
    op.execute(_LEGACY_POSTING_SHAPE_SQL)
    op.execute("DROP FUNCTION coinpup_validate_shape_exchange(uuid, jsonb)")
    op.execute("DROP FUNCTION coinpup_validate_shape_transfer(uuid, jsonb)")
    op.execute("DROP FUNCTION coinpup_validate_shape_expense(uuid, jsonb)")
    op.execute("DROP FUNCTION coinpup_validate_shape_income(uuid, jsonb)")
    op.execute("DROP FUNCTION coinpup_validate_shape_opening(uuid, jsonb)")
    op.execute("DROP FUNCTION coinpup_validate_journal_common(uuid)")
