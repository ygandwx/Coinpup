"""Same-asset transfers preserve the immutable initial posting contract."""

from alembic import op

revision = "20261003_0005"
down_revision = "20261003_0004"
branch_labels = None
depends_on = None

# Both function bodies are frozen here. Replaying or reversing this migration must
# never import current application code or depend on mutable migration helpers.
_PREVIOUS_VALIDATOR_SQL = """
        CREATE OR REPLACE FUNCTION coinpup_validate_initial_journal(journal_identifier uuid)
        RETURNS void
        LANGUAGE plpgsql SET search_path = pg_catalog AS $$
        DECLARE header public.journals%ROWTYPE; operation public.financial_operations%ROWTYPE;
                line_count integer; account_count integer; asset_count integer;
        BEGIN
            SELECT * INTO header FROM public.journals WHERE id = journal_identifier FOR UPDATE;
            IF NOT FOUND THEN
                RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'Journal is missing',
                    CONSTRAINT = 'ck_journal_shape';
            END IF;
            SELECT * INTO operation FROM public.financial_operations WHERE id = header.operation_id;
            IF NOT FOUND OR operation.ledger_id <> header.ledger_id OR operation.version <> 1 OR
               header.operation_version <> 1 OR header.journal_kind <> 'posting' OR
               header.reverses_journal_id IS NOT NULL OR
               operation.current_journal_id <> header.id THEN
                RAISE EXCEPTION USING ERRCODE = '23514',
                    MESSAGE = 'Initial journal identity is invalid',
                    CONSTRAINT = 'ck_journal_shape';
            END IF;
            SELECT count(*), count(*) FILTER (WHERE role = 'account'), count(DISTINCT asset_id)
                INTO line_count, account_count, asset_count FROM public.journal_lines
                WHERE journal_id = journal_identifier;
            IF line_count < 2 OR account_count <> 1 OR asset_count <> 1 THEN
                RAISE EXCEPTION USING ERRCODE = '23514',
                    MESSAGE = 'Initial journal shape is invalid',
                    CONSTRAINT = 'ck_journal_shape';
            END IF;
            IF EXISTS (
                SELECT 1 FROM public.journal_lines WHERE journal_id = journal_identifier
                GROUP BY asset_id HAVING sum(amount) <> 0
            ) THEN
                RAISE EXCEPTION USING ERRCODE = '23514',
                    MESSAGE = 'Journal does not balance by asset',
                    CONSTRAINT = 'ck_journal_balanced';
            END IF;
            IF operation.kind = 'opening' THEN
                IF line_count <> 2 OR header.recognition_date <> header.transaction_date OR
                   (SELECT count(*) FROM public.journal_lines WHERE journal_id = journal_identifier
                        AND role = 'equity') <> 1 THEN
                    RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'Opening shape is invalid',
                        CONSTRAINT = 'ck_journal_shape';
                END IF;
                IF NOT EXISTS (
                    SELECT 1 FROM public.opening_positions opening
                    JOIN public.journal_lines line ON line.journal_id = journal_identifier
                        AND line.role = 'account' AND line.account_id = opening.account_id
                        AND line.asset_id = opening.asset_id AND line.ledger_id = opening.ledger_id
                    WHERE opening.operation_id = operation.id
                        AND opening.ledger_id = header.ledger_id
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
                    SELECT 1 FROM public.journal_lines WHERE journal_id = journal_identifier AND
                        ((role = 'account' AND amount >= 0) OR
                         (role <> 'account' AND (role <> 'expense' OR amount <= 0)))
                ) THEN
                    RAISE EXCEPTION USING ERRCODE = '23514',
                        MESSAGE = 'Expense signs or roles are invalid',
                        CONSTRAINT = 'ck_journal_shape';
                END IF;
                IF operation.kind = 'income' AND EXISTS (
                    SELECT 1 FROM public.journal_lines WHERE journal_id = journal_identifier AND
                        ((role = 'account' AND amount <= 0) OR
                         (role <> 'account' AND (role <> 'income' OR amount >= 0)))
                ) THEN
                    RAISE EXCEPTION USING ERRCODE = '23514',
                        MESSAGE = 'Income signs or roles are invalid',
                        CONSTRAINT = 'ck_journal_shape';
                END IF;
            END IF;
            IF NOT header.sealed THEN
                UPDATE public.journals SET sealed = true WHERE id = journal_identifier;
            END IF;
        END;
        $$
    """

_TRANSFER_VALIDATOR_SQL = """
        CREATE OR REPLACE FUNCTION coinpup_validate_initial_journal(journal_identifier uuid)
        RETURNS void
        LANGUAGE plpgsql SET search_path = pg_catalog AS $$
        DECLARE header public.journals%ROWTYPE; operation public.financial_operations%ROWTYPE;
                line_count integer; account_count integer; asset_count integer;
        BEGIN
            SELECT * INTO header FROM public.journals WHERE id = journal_identifier FOR UPDATE;
            IF NOT FOUND THEN
                RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'Journal is missing',
                    CONSTRAINT = 'ck_journal_shape';
            END IF;
            SELECT * INTO operation FROM public.financial_operations WHERE id = header.operation_id;
            IF NOT FOUND OR operation.ledger_id <> header.ledger_id OR operation.version <> 1 OR
               header.operation_version <> 1 OR header.journal_kind <> 'posting' OR
               header.reverses_journal_id IS NOT NULL OR
               operation.current_journal_id <> header.id THEN
                RAISE EXCEPTION USING ERRCODE = '23514',
                    MESSAGE = 'Initial journal identity is invalid',
                    CONSTRAINT = 'ck_journal_shape';
            END IF;
            SELECT count(*), count(*) FILTER (WHERE role = 'account'), count(DISTINCT asset_id)
                INTO line_count, account_count, asset_count FROM public.journal_lines
                WHERE journal_id = journal_identifier;
            IF operation.kind = 'transfer' THEN
                IF line_count <> 2 OR account_count <> 2 OR asset_count <> 1 OR
                   (SELECT count(DISTINCT account_id) FROM public.journal_lines
                        WHERE journal_id = journal_identifier) <> 2 THEN
                    RAISE EXCEPTION USING ERRCODE = '23514',
                        MESSAGE = 'Transfer requires two distinct accounts of the same asset',
                        CONSTRAINT = 'ck_journal_shape';
                END IF;
                IF header.recognition_date <> header.transaction_date THEN
                    RAISE EXCEPTION USING ERRCODE = '23514',
                        MESSAGE = 'Transfer dates must match', CONSTRAINT = 'ck_journal_shape';
                END IF;
            ELSIF line_count < 2 OR account_count <> 1 OR asset_count <> 1 THEN
                RAISE EXCEPTION USING ERRCODE = '23514',
                    MESSAGE = 'Initial journal shape is invalid',
                    CONSTRAINT = 'ck_journal_shape';
            END IF;
            IF EXISTS (
                SELECT 1 FROM public.journal_lines WHERE journal_id = journal_identifier
                GROUP BY asset_id HAVING sum(amount) <> 0
            ) THEN
                RAISE EXCEPTION USING ERRCODE = '23514',
                    MESSAGE = 'Journal does not balance by asset',
                    CONSTRAINT = 'ck_journal_balanced';
            END IF;
            IF operation.kind = 'opening' THEN
                IF line_count <> 2 OR header.recognition_date <> header.transaction_date OR
                   (SELECT count(*) FROM public.journal_lines WHERE journal_id = journal_identifier
                        AND role = 'equity') <> 1 THEN
                    RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'Opening shape is invalid',
                        CONSTRAINT = 'ck_journal_shape';
                END IF;
                IF NOT EXISTS (
                    SELECT 1 FROM public.opening_positions opening
                    JOIN public.journal_lines line ON line.journal_id = journal_identifier
                        AND line.role = 'account' AND line.account_id = opening.account_id
                        AND line.asset_id = opening.asset_id AND line.ledger_id = opening.ledger_id
                    WHERE opening.operation_id = operation.id
                        AND opening.ledger_id = header.ledger_id
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
                    SELECT 1 FROM public.journal_lines WHERE journal_id = journal_identifier AND
                        ((role = 'account' AND amount >= 0) OR
                         (role <> 'account' AND (role <> 'expense' OR amount <= 0)))
                ) THEN
                    RAISE EXCEPTION USING ERRCODE = '23514',
                        MESSAGE = 'Expense signs or roles are invalid',
                        CONSTRAINT = 'ck_journal_shape';
                END IF;
                IF operation.kind = 'income' AND EXISTS (
                    SELECT 1 FROM public.journal_lines WHERE journal_id = journal_identifier AND
                        ((role = 'account' AND amount <= 0) OR
                         (role <> 'account' AND (role <> 'income' OR amount >= 0)))
                ) THEN
                    RAISE EXCEPTION USING ERRCODE = '23514',
                        MESSAGE = 'Income signs or roles are invalid',
                        CONSTRAINT = 'ck_journal_shape';
                END IF;
            END IF;
            IF NOT header.sealed THEN
                UPDATE public.journals SET sealed = true WHERE id = journal_identifier;
            END IF;
        END;
        $$
    """


def upgrade() -> None:
    op.drop_constraint("ck_financial_operations_kind", "financial_operations", type_="check")
    op.create_check_constraint(
        "ck_financial_operations_kind",
        "financial_operations",
        "kind IN ('opening', 'income', 'expense', 'transfer')",
    )
    op.execute(_TRANSFER_VALIDATOR_SQL)


def downgrade() -> None:
    # Serialize the preflight and check replacement against new financial operations.
    # A rollback must preserve financial history, never delete unsupported records.
    op.execute("LOCK TABLE public.financial_operations IN ACCESS EXCLUSIVE MODE")
    op.execute("""
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM public.financial_operations WHERE kind = 'transfer') THEN
                RAISE EXCEPTION USING ERRCODE = '23514',
                    MESSAGE = 'Cannot downgrade while transfer history exists',
                    CONSTRAINT = 'ck_transfer_downgrade';
            END IF;
        END;
        $$
    """)
    op.drop_constraint("ck_financial_operations_kind", "financial_operations", type_="check")
    op.create_check_constraint(
        "ck_financial_operations_kind",
        "financial_operations",
        "kind IN ('opening', 'income', 'expense')",
    )
    op.execute(_PREVIOUS_VALIDATOR_SQL)
