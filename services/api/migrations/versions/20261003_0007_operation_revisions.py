"""Append-only corrections and cancellation with complete version-chain validation."""

import sqlalchemy as sa
from alembic import op

revision = "20261003_0007"
down_revision = "20261003_0006"
branch_labels = None
depends_on = None

# Both SQL bodies are frozen: downgrade and replay never import live business modules.
_PREVIOUS_VALIDATOR_SQL = """
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

_POSTING_SHAPE_SQL = """
    CREATE FUNCTION coinpup_validate_posting_shape(journal_identifier uuid)
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

_REVERSAL_SQL = """
    CREATE FUNCTION coinpup_validate_reversal(journal_identifier uuid) RETURNS void
    LANGUAGE plpgsql SET search_path = pg_catalog AS $$
    DECLARE reversal public.journals%ROWTYPE; source public.journals%ROWTYPE;
    BEGIN
        SELECT * INTO reversal FROM public.journals WHERE id = journal_identifier;
        IF NOT FOUND OR reversal.journal_kind <> 'reversal' OR reversal.operation_version < 2 THEN
            RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'Reversal identity is invalid',
                CONSTRAINT = 'ck_journal_reversal';
        END IF;
        SELECT * INTO source FROM public.journals WHERE id = reversal.reverses_journal_id;
        IF NOT FOUND OR source.journal_kind <> 'posting' OR NOT source.sealed OR
           source.operation_id <> reversal.operation_id OR source.ledger_id <> reversal.ledger_id OR
           source.operation_version <> reversal.operation_version - 1 OR
           source.transaction_date IS DISTINCT FROM reversal.transaction_date OR
           source.recognition_date IS DISTINCT FROM reversal.recognition_date OR
           source.description IS DISTINCT FROM reversal.description THEN
            RAISE EXCEPTION USING ERRCODE = '23514',
                MESSAGE = 'Reversal must reference the immediately preceding posting',
                CONSTRAINT = 'ck_journal_reversal';
        END IF;
        IF EXISTS (
            SELECT line_no, component_no, role, asset_id, account_id, category_id, amount
            FROM public.journal_lines WHERE journal_id = reversal.id
            EXCEPT
            SELECT line_no, component_no, role, asset_id, account_id, category_id, -amount
            FROM public.journal_lines WHERE journal_id = source.id
        ) OR EXISTS (
            SELECT line_no, component_no, role, asset_id, account_id, category_id, -amount
            FROM public.journal_lines WHERE journal_id = source.id
            EXCEPT
            SELECT line_no, component_no, role, asset_id, account_id, category_id, amount
            FROM public.journal_lines WHERE journal_id = reversal.id
        ) THEN
            RAISE EXCEPTION USING ERRCODE = '23514',
                MESSAGE = 'Reversal lines must be the exact inverse of the original posting',
                CONSTRAINT = 'ck_journal_reversal';
        END IF;
    END;
    $$
"""

_HISTORY_SQL = """
    CREATE FUNCTION coinpup_validate_operation_history(operation_identifier uuid) RETURNS void
    LANGUAGE plpgsql SET search_path = pg_catalog AS $$
    DECLARE operation public.financial_operations%ROWTYPE;
            posting public.journals%ROWTYPE; reversal public.journals%ROWTYPE;
            posting_exists boolean; reversal_exists boolean;
            revision integer; journal_count bigint; expected_count bigint;
            previous_posting uuid;
    BEGIN
        SELECT * INTO operation FROM public.financial_operations
            WHERE id = operation_identifier FOR UPDATE;
        IF NOT FOUND THEN
            RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'Operation is missing',
                CONSTRAINT = 'ck_operation_history';
        END IF;
        IF operation.status = 'cancelled' AND operation.version < 2 THEN
            RAISE EXCEPTION USING ERRCODE = '23514',
                MESSAGE = 'Initial operation cannot be cancelled',
                CONSTRAINT = 'ck_operation_history';
        END IF;
        SELECT count(*) INTO journal_count FROM public.journals WHERE operation_id = operation.id;
        expected_count := operation.version::bigint * 2 -
            CASE WHEN operation.status = 'active' THEN 1 ELSE 2 END;
        IF journal_count <> expected_count THEN
            RAISE EXCEPTION USING ERRCODE = '23514',
                MESSAGE = 'Operation revision chain is incomplete',
                CONSTRAINT = 'ck_operation_history';
        END IF;
        FOR revision IN 1..operation.version LOOP
            SELECT * INTO posting FROM public.journals WHERE operation_id = operation.id
                AND operation_version = revision AND journal_kind = 'posting';
            posting_exists := FOUND;
            SELECT * INTO reversal FROM public.journals WHERE operation_id = operation.id
                AND operation_version = revision AND journal_kind = 'reversal';
            reversal_exists := FOUND;
            IF revision = 1 THEN
                IF NOT posting_exists OR reversal_exists THEN
                    RAISE EXCEPTION USING ERRCODE = '23514',
                        MESSAGE = 'First revision requires only one posting',
                        CONSTRAINT = 'ck_operation_history';
                END IF;
            ELSE
                IF NOT reversal_exists OR reversal.reverses_journal_id <> previous_posting THEN
                    RAISE EXCEPTION USING ERRCODE = '23514',
                        MESSAGE = 'Each revision must reverse its preceding posting',
                        CONSTRAINT = 'ck_operation_history';
                END IF;
                PERFORM public.coinpup_validate_reversal(reversal.id);
                IF NOT reversal.sealed THEN
                    UPDATE public.journals SET sealed = true WHERE id = reversal.id;
                END IF;
                IF operation.status = 'cancelled' AND revision = operation.version THEN
                    IF posting_exists THEN
                        RAISE EXCEPTION USING ERRCODE = '23514',
                            MESSAGE = 'Cancellation cannot contain a replacement posting',
                            CONSTRAINT = 'ck_operation_history';
                    END IF;
                    CONTINUE;
                END IF;
                IF NOT posting_exists OR
                   posting.revision_reason IS DISTINCT FROM reversal.revision_reason THEN
                    RAISE EXCEPTION USING ERRCODE = '23514',
                        MESSAGE = 'Correction requires a replacement with the same reason',
                        CONSTRAINT = 'ck_operation_history';
                END IF;
            END IF;
            PERFORM public.coinpup_validate_posting_shape(posting.id);
            IF NOT posting.sealed THEN
                UPDATE public.journals SET sealed = true WHERE id = posting.id;
            END IF;
            previous_posting := posting.id;
        END LOOP;
        IF operation.current_journal_id IS DISTINCT FROM previous_posting THEN
            RAISE EXCEPTION USING ERRCODE = '23514',
                MESSAGE = 'Current journal must remain the latest posting',
                CONSTRAINT = 'ck_operation_history';
        END IF;
    END;
    $$
"""

_OPERATION_GUARD_SQL = """
    CREATE FUNCTION coinpup_operation_revision_guard() RETURNS trigger
    LANGUAGE plpgsql SET search_path = pg_catalog AS $$
    BEGIN
        IF TG_OP = 'INSERT' THEN
            IF NEW.version IS DISTINCT FROM 1 OR NEW.status IS DISTINCT FROM 'active' THEN
                RAISE EXCEPTION USING ERRCODE = '23514',
                    MESSAGE = 'Operations start active at version one',
                    CONSTRAINT = 'ck_operation_history';
            END IF;
            RETURN NEW;
        END IF;
        IF TG_OP = 'DELETE' THEN
            RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'Operations cannot be deleted',
                CONSTRAINT = 'ck_posting_immutable';
        END IF;
        IF OLD.status <> 'active' OR NEW.version IS DISTINCT FROM OLD.version + 1 OR
           NEW.status NOT IN ('active', 'cancelled') OR
           (NEW.status = 'active' AND
                NEW.current_journal_id IS NOT DISTINCT FROM OLD.current_journal_id) OR
           (NEW.status = 'cancelled' AND
                NEW.current_journal_id IS DISTINCT FROM OLD.current_journal_id) OR
           (to_jsonb(NEW) - ARRAY['version','status','current_journal_id','updated_at'])
               IS DISTINCT FROM
           (to_jsonb(OLD) - ARRAY['version','status','current_journal_id','updated_at']) THEN
            RAISE EXCEPTION USING ERRCODE = '23514',
                MESSAGE = 'Only an active operation may advance to its next revision',
                CONSTRAINT = 'ck_posting_immutable';
        END IF;
        NEW.updated_at := clock_timestamp();
        RETURN NEW;
    END;
    $$
"""


def upgrade() -> None:
    op.execute(
        "LOCK TABLE public.financial_operations, public.journals, public.journal_lines "
        "IN ACCESS EXCLUSIVE MODE"
    )
    op.add_column(
        "financial_operations",
        sa.Column("status", sa.String(16), nullable=False, server_default="active"),
    )
    op.create_check_constraint(
        "ck_financial_operations_status",
        "financial_operations",
        "status IN ('active', 'cancelled')",
    )
    op.add_column("journals", sa.Column("revision_reason", sa.String(1000), nullable=True))
    op.create_check_constraint(
        "ck_journals_revision_reason",
        "journals",
        "(operation_version = 1 AND revision_reason IS NULL) OR "
        "(operation_version > 1 AND revision_reason IS NOT NULL "
        "AND revision_reason = btrim(revision_reason) AND revision_reason ~ '[^[:space:]]')",
    )
    op.execute(_POSTING_SHAPE_SQL)
    op.execute(_REVERSAL_SQL)
    op.execute(_HISTORY_SQL)
    op.execute(_OPERATION_GUARD_SQL)
    op.execute("DROP TRIGGER tr_financial_operations_immutable ON financial_operations")
    op.execute(
        "CREATE TRIGGER tr_financial_operations_guard BEFORE INSERT OR UPDATE OR DELETE "
        "ON financial_operations FOR EACH ROW EXECUTE FUNCTION coinpup_operation_revision_guard()"
    )
    op.execute("""
        CREATE OR REPLACE FUNCTION coinpup_validate_initial_journal(journal_identifier uuid)
        RETURNS void LANGUAGE plpgsql SET search_path = pg_catalog AS $$
        DECLARE operation_identifier uuid;
        BEGIN
            SELECT operation_id INTO operation_identifier FROM public.journals
                WHERE id = journal_identifier;
            IF NOT FOUND THEN
                RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'Journal is missing',
                    CONSTRAINT = 'ck_journal_shape';
            END IF;
            PERFORM public.coinpup_validate_operation_history(operation_identifier);
        END;
        $$
    """)
    op.execute("""
        CREATE FUNCTION coinpup_operation_deferred_check() RETURNS trigger
        LANGUAGE plpgsql SET search_path = pg_catalog AS $$
        BEGIN
            PERFORM public.coinpup_validate_operation_history(NEW.id);
            RETURN NULL;
        END;
        $$
    """)
    op.execute(
        "CREATE CONSTRAINT TRIGGER tr_financial_operations_deferred "
        "AFTER INSERT OR UPDATE ON financial_operations DEFERRABLE INITIALLY DEFERRED "
        "FOR EACH ROW EXECUTE FUNCTION coinpup_operation_deferred_check()"
    )


def downgrade() -> None:
    op.execute(
        "LOCK TABLE public.financial_operations, public.journals, public.journal_lines "
        "IN ACCESS EXCLUSIVE MODE"
    )
    op.execute("""
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM public.financial_operations WHERE version > 1 OR status <> 'active'
            ) OR EXISTS (
                SELECT 1 FROM public.journals WHERE operation_version > 1 OR
                    journal_kind = 'reversal' OR revision_reason IS NOT NULL
            ) THEN
                RAISE EXCEPTION USING ERRCODE = '23514',
                    MESSAGE = 'Cannot downgrade while operation revision history exists',
                    CONSTRAINT = 'ck_revision_downgrade';
            END IF;
        END;
        $$
    """)
    op.execute("DROP TRIGGER tr_financial_operations_deferred ON financial_operations")
    op.execute("DROP TRIGGER tr_financial_operations_guard ON financial_operations")
    op.execute(
        "CREATE TRIGGER tr_financial_operations_immutable BEFORE UPDATE OR DELETE "
        "ON financial_operations FOR EACH ROW EXECUTE FUNCTION coinpup_posting_reject_change()"
    )
    op.execute(_PREVIOUS_VALIDATOR_SQL)
    op.execute("DROP FUNCTION coinpup_operation_deferred_check()")
    op.execute("DROP FUNCTION coinpup_operation_revision_guard()")
    op.execute("DROP FUNCTION coinpup_validate_operation_history(uuid)")
    op.execute("DROP FUNCTION coinpup_validate_reversal(uuid)")
    op.execute("DROP FUNCTION coinpup_validate_posting_shape(uuid)")
    op.drop_constraint("ck_journals_revision_reason", "journals", type_="check")
    op.drop_column("journals", "revision_reason")
    op.drop_constraint("ck_financial_operations_status", "financial_operations", type_="check")
    op.drop_column("financial_operations", "status")
