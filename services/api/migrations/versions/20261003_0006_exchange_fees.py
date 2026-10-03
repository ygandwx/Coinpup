"""Principal exchange quantities and separately balanced fee components."""

import sqlalchemy as sa
from alembic import op

revision = "20261003_0006"
down_revision = "20261003_0005"
branch_labels = None
depends_on = None

# Frozen previous body keeps downgrades independent of live application code.
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

_EXCHANGE_VALIDATOR_SQL = """
    CREATE OR REPLACE FUNCTION coinpup_validate_initial_journal(journal_identifier uuid)
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
        IF NOT FOUND OR operation.ledger_id <> header.ledger_id OR operation.version <> 1 OR
           header.operation_version <> 1 OR header.journal_kind <> 'posting' OR
           header.reverses_journal_id IS NOT NULL OR operation.current_journal_id <> header.id THEN
            RAISE EXCEPTION USING ERRCODE = '23514',
                MESSAGE = 'Initial journal identity is invalid', CONSTRAINT = 'ck_journal_shape';
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
        IF NOT header.sealed THEN
            UPDATE public.journals SET sealed = true WHERE id = journal_identifier;
        END IF;
    END;
    $$
"""


def upgrade() -> None:
    # Match posting lock order while changing both the operation and line contracts.
    op.execute(
        "LOCK TABLE public.financial_operations, public.journal_lines IN ACCESS EXCLUSIVE MODE"
    )
    op.add_column(
        "journal_lines",
        sa.Column("component_no", sa.Integer(), nullable=False, server_default="0"),
    )
    op.create_check_constraint(
        "ck_journal_lines_component", "journal_lines", "component_no BETWEEN 0 AND 20"
    )
    op.drop_constraint("ck_journal_lines_role", "journal_lines", type_="check")
    op.create_check_constraint(
        "ck_journal_lines_role",
        "journal_lines",
        "(role = 'account' AND account_id IS NOT NULL AND category_id IS NULL) OR "
        "(role IN ('income', 'expense') AND account_id IS NULL AND category_id IS NOT NULL) OR "
        "(role IN ('equity', 'exchange') AND account_id IS NULL AND category_id IS NULL)",
    )
    op.drop_constraint("ck_financial_operations_kind", "financial_operations", type_="check")
    op.create_check_constraint(
        "ck_financial_operations_kind",
        "financial_operations",
        "kind IN ('opening', 'income', 'expense', 'transfer', 'exchange')",
    )
    op.execute(_EXCHANGE_VALIDATOR_SQL)


def downgrade() -> None:
    op.execute(
        "LOCK TABLE public.financial_operations, public.journal_lines IN ACCESS EXCLUSIVE MODE"
    )
    op.execute("""
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM public.financial_operations WHERE kind = 'exchange') OR
               EXISTS (SELECT 1 FROM public.journal_lines WHERE component_no > 0) THEN
                RAISE EXCEPTION USING ERRCODE = '23514',
                    MESSAGE = 'Cannot downgrade while exchange or fee history exists',
                    CONSTRAINT = 'ck_exchange_downgrade';
            END IF;
        END;
        $$
    """)
    op.execute(_PREVIOUS_VALIDATOR_SQL)
    op.drop_constraint("ck_financial_operations_kind", "financial_operations", type_="check")
    op.create_check_constraint(
        "ck_financial_operations_kind",
        "financial_operations",
        "kind IN ('opening', 'income', 'expense', 'transfer')",
    )
    op.drop_constraint("ck_journal_lines_role", "journal_lines", type_="check")
    op.create_check_constraint(
        "ck_journal_lines_role",
        "journal_lines",
        "(role = 'account' AND account_id IS NOT NULL AND category_id IS NULL) OR "
        "(role IN ('income', 'expense') AND account_id IS NULL AND category_id IS NOT NULL) OR "
        "(role = 'equity' AND account_id IS NULL AND category_id IS NULL)",
    )
    op.drop_constraint("ck_journal_lines_component", "journal_lines", type_="check")
    op.drop_column("journal_lines", "component_no")
