"""Atomic initial postings, immutable sealed journals and replay receipts."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "20261003_0004"
down_revision = "20261003_0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_unique_constraint("uq_ledgers_id_owner", "ledgers", ["id", "owner_id"])
    op.create_unique_constraint(
        "uq_account_assets_account_asset_ledger",
        "account_assets",
        ["account_id", "asset_id", "ledger_id"],
    )
    op.create_table(
        "financial_operations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("ledger_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("current_journal_id", sa.Uuid(), nullable=False),
        sa.Column("created_by", sa.Uuid(), nullable=False),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "kind IN ('opening', 'income', 'expense')", name="ck_financial_operations_kind"
        ),
        sa.CheckConstraint("version > 0", name="ck_financial_operations_version"),
        sa.ForeignKeyConstraint(
            ["ledger_id", "created_by"],
            ["ledgers.id", "ledgers.owner_id"],
            name="fk_financial_operations_ledger_owner",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("id", "ledger_id", name="uq_financial_operations_id_ledger"),
    )
    op.create_index(
        op.f("ix_financial_operations_ledger_id"),
        "financial_operations",
        ["ledger_id"],
        unique=False,
    )
    op.create_table(
        "journals",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("operation_id", sa.Uuid(), nullable=False),
        sa.Column("ledger_id", sa.Uuid(), nullable=False),
        sa.Column("operation_version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("journal_kind", sa.String(length=16), server_default="posting", nullable=False),
        sa.Column("reverses_journal_id", sa.Uuid(), nullable=True),
        sa.Column("transaction_date", sa.Date(), nullable=False),
        sa.Column("recognition_date", sa.Date(), nullable=False),
        sa.Column("description", sa.String(length=2000), server_default="", nullable=False),
        sa.Column("sealed", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "(journal_kind = 'posting' AND reverses_journal_id IS NULL) OR "
            "(journal_kind = 'reversal' AND reverses_journal_id IS NOT NULL)",
            name="ck_journals_kind",
        ),
        sa.CheckConstraint("operation_version > 0", name="ck_journals_operation_version"),
        sa.CheckConstraint(
            "reverses_journal_id IS NULL OR reverses_journal_id <> id",
            name="ck_journals_self_reversal",
        ),
        sa.ForeignKeyConstraint(
            ["operation_id", "ledger_id"],
            ["financial_operations.id", "financial_operations.ledger_id"],
            name="fk_journals_operation_ledger",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["reverses_journal_id", "operation_id", "ledger_id"],
            ["journals.id", "journals.operation_id", "journals.ledger_id"],
            name="fk_journals_reverses_operation_ledger",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("id", "ledger_id", name="uq_journals_id_ledger"),
        sa.UniqueConstraint(
            "id", "operation_id", "ledger_id", name="uq_journals_id_operation_ledger"
        ),
        sa.UniqueConstraint(
            "operation_id",
            "operation_version",
            "journal_kind",
            name="uq_journals_operation_revision",
        ),
        sa.UniqueConstraint("reverses_journal_id", name="uq_journals_reversal"),
    )
    op.create_index(op.f("ix_journals_ledger_id"), "journals", ["ledger_id"], unique=False)
    op.create_table(
        "journal_lines",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("journal_id", sa.Uuid(), nullable=False),
        sa.Column("ledger_id", sa.Uuid(), nullable=False),
        sa.Column("line_no", sa.Integer(), nullable=False),
        sa.Column("role", sa.String(length=16), nullable=False),
        sa.Column("asset_id", sa.String(length=200), nullable=False),
        sa.Column("amount", sa.Numeric(precision=38, scale=18), nullable=False),
        sa.Column("account_id", sa.Uuid(), nullable=True),
        sa.Column("category_id", sa.Uuid(), nullable=True),
        sa.CheckConstraint(
            "(role = 'account' AND account_id IS NOT NULL AND category_id IS NULL) OR "
            "(role IN ('income', 'expense') AND account_id IS NULL AND category_id IS NOT NULL) OR "
            "(role = 'equity' AND account_id IS NULL AND category_id IS NULL)",
            name="ck_journal_lines_role",
        ),
        sa.CheckConstraint(
            "amount <> 0 AND amount > -100000000000000000000 AND amount < 100000000000000000000",
            name="ck_journal_lines_amount",
        ),
        sa.CheckConstraint("line_no > 0", name="ck_journal_lines_number"),
        sa.ForeignKeyConstraint(
            ["account_id", "asset_id", "ledger_id"],
            ["account_assets.account_id", "account_assets.asset_id", "account_assets.ledger_id"],
            name="fk_journal_lines_account_asset_ledger",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["asset_id"], ["assets.asset_id"], name="fk_journal_lines_asset", ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["category_id", "ledger_id", "role"],
            ["categories.id", "categories.ledger_id", "categories.kind"],
            name="fk_journal_lines_category_ledger_kind",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["journal_id", "ledger_id"],
            ["journals.id", "journals.ledger_id"],
            name="fk_journal_lines_journal_ledger",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("journal_id", "line_no", name="uq_journal_lines_number"),
    )
    op.create_index(
        op.f("ix_journal_lines_journal_id"), "journal_lines", ["journal_id"], unique=False
    )
    op.create_index(
        op.f("ix_journal_lines_ledger_id"), "journal_lines", ["ledger_id"], unique=False
    )
    op.create_table(
        "opening_positions",
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("asset_id", sa.String(length=200), nullable=False),
        sa.Column("ledger_id", sa.Uuid(), nullable=False),
        sa.Column("operation_id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(
            ["account_id", "asset_id", "ledger_id"],
            ["account_assets.account_id", "account_assets.asset_id", "account_assets.ledger_id"],
            name="fk_opening_positions_account_asset_ledger",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["operation_id", "ledger_id"],
            ["financial_operations.id", "financial_operations.ledger_id"],
            name="fk_opening_positions_operation_ledger",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("account_id", "asset_id"),
        sa.UniqueConstraint("operation_id", name="uq_opening_positions_operation"),
    )
    op.create_table(
        "command_receipts",
        sa.Column("ledger_id", sa.Uuid(), nullable=False),
        sa.Column("key", sa.String(length=128), nullable=False),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("response", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("response_status", sa.Integer(), nullable=False),
        sa.Column("operation_id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "jsonb_typeof(response) = 'object'", name="ck_command_receipts_response"
        ),
        sa.CheckConstraint("key <> ''", name="ck_command_receipts_key"),
        sa.CheckConstraint("request_hash ~ '^[0-9a-f]{64}$'", name="ck_command_receipts_hash"),
        sa.CheckConstraint(
            "response_status BETWEEN 200 AND 299", name="ck_command_receipts_status"
        ),
        sa.ForeignKeyConstraint(
            ["operation_id", "ledger_id"],
            ["financial_operations.id", "financial_operations.ledger_id"],
            name="fk_command_receipts_operation_ledger",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("ledger_id", "key"),
    )
    op.create_foreign_key(
        "fk_financial_operations_current_journal",
        "financial_operations",
        "journals",
        ["current_journal_id", "id", "ledger_id"],
        ["id", "operation_id", "ledger_id"],
        initially="DEFERRED",
        deferrable=True,
        use_alter=True,
    )
    op.execute("""
        CREATE FUNCTION coinpup_posting_reject_change() RETURNS trigger
        LANGUAGE plpgsql SET search_path = pg_catalog AS $$
        BEGIN
            RAISE EXCEPTION USING ERRCODE = '23514',
                MESSAGE = 'Posting records are immutable', CONSTRAINT = 'ck_posting_immutable';
        END;
        $$
    """)
    for table in ("financial_operations", "journal_lines", "opening_positions", "command_receipts"):
        op.execute(
            f"CREATE TRIGGER tr_{table}_immutable BEFORE UPDATE OR DELETE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION coinpup_posting_reject_change()"
        )
    op.execute("""
        CREATE FUNCTION coinpup_journal_header_guard() RETURNS trigger
        LANGUAGE plpgsql SET search_path = pg_catalog AS $$
        BEGIN
            IF TG_OP = 'INSERT' THEN
                NEW.sealed := false;
                RETURN NEW;
            END IF;
            IF TG_OP = 'UPDATE' AND NOT OLD.sealed AND NEW.sealed AND
               (to_jsonb(NEW) - 'sealed') IS NOT DISTINCT FROM (to_jsonb(OLD) - 'sealed') THEN
                RETURN NEW;
            END IF;
            RAISE EXCEPTION USING ERRCODE = '23514',
                MESSAGE = 'Journal headers are immutable', CONSTRAINT = 'ck_posting_immutable';
        END;
        $$
    """)
    op.execute(
        "CREATE TRIGGER tr_journals_guard BEFORE INSERT OR UPDATE OR DELETE ON journals "
        "FOR EACH ROW EXECUTE FUNCTION coinpup_journal_header_guard()"
    )
    op.execute("""
        CREATE FUNCTION coinpup_journal_line_guard() RETURNS trigger
        LANGUAGE plpgsql SET search_path = pg_catalog AS $$
        DECLARE header public.journals%ROWTYPE; asset_scale integer;
        BEGIN
            SELECT * INTO header FROM public.journals WHERE id = NEW.journal_id FOR UPDATE;
            IF NOT FOUND OR header.ledger_id IS DISTINCT FROM NEW.ledger_id THEN
                RAISE EXCEPTION USING ERRCODE = '23503',
                    MESSAGE = 'Journal must exist in this ledger',
                    CONSTRAINT = 'fk_journal_lines_journal_ledger';
            END IF;
            IF header.sealed THEN
                RAISE EXCEPTION USING ERRCODE = '23514',
                    MESSAGE = 'A sealed journal cannot receive additional lines',
                    CONSTRAINT = 'ck_journal_sealed';
            END IF;
            SELECT scale INTO asset_scale FROM public.assets WHERE asset_id = NEW.asset_id;
            IF NOT FOUND THEN
                RAISE EXCEPTION USING ERRCODE = '23503', MESSAGE = 'Asset does not exist',
                    CONSTRAINT = 'fk_journal_lines_asset';
            END IF;
            IF NEW.amount <> trunc(NEW.amount, asset_scale) THEN
                RAISE EXCEPTION USING ERRCODE = '23514',
                    MESSAGE = 'Quantity exceeds the asset precision',
                    CONSTRAINT = 'ck_journal_line_precision';
            END IF;
            RETURN NEW;
        END;
        $$
    """)
    op.execute(
        "CREATE TRIGGER tr_journal_lines_guard BEFORE INSERT ON journal_lines "
        "FOR EACH ROW EXECUTE FUNCTION coinpup_journal_line_guard()"
    )
    # NUMERIC's typmod is applied before row triggers. The application also validates
    # Amount against its row asset before SQL binding, including the 19th decimal place.
    op.execute("""
        CREATE FUNCTION coinpup_validate_initial_journal(journal_identifier uuid) RETURNS void
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
    """)
    op.execute("""
        CREATE FUNCTION coinpup_journal_deferred_check() RETURNS trigger
        LANGUAGE plpgsql SET search_path = pg_catalog AS $$
        DECLARE journal_identifier uuid;
        BEGIN
            IF TG_TABLE_NAME = 'journals' THEN
                journal_identifier := NEW.id;
            ELSIF TG_TABLE_NAME = 'journal_lines' THEN
                journal_identifier := NEW.journal_id;
            ELSE
                SELECT current_journal_id INTO journal_identifier FROM public.financial_operations
                    WHERE id = NEW.operation_id;
            END IF;
            PERFORM public.coinpup_validate_initial_journal(journal_identifier);
            RETURN NULL;
        END;
        $$
    """)
    for table in ("journals", "journal_lines", "opening_positions"):
        op.execute(
            f"CREATE CONSTRAINT TRIGGER tr_{table}_deferred AFTER INSERT ON {table} "
            "DEFERRABLE INITIALLY DEFERRED FOR EACH ROW "
            "EXECUTE FUNCTION coinpup_journal_deferred_check()"
        )


def downgrade() -> None:
    op.drop_constraint(
        "fk_financial_operations_current_journal", "financial_operations", type_="foreignkey"
    )
    for table in (
        "command_receipts",
        "opening_positions",
        "journal_lines",
        "journals",
        "financial_operations",
    ):
        op.drop_table(table)
    op.execute("DROP FUNCTION coinpup_journal_deferred_check()")
    op.execute("DROP FUNCTION coinpup_validate_initial_journal(uuid)")
    op.execute("DROP FUNCTION coinpup_journal_line_guard()")
    op.execute("DROP FUNCTION coinpup_journal_header_guard()")
    op.execute("DROP FUNCTION coinpup_posting_reject_change()")
    op.drop_constraint("uq_account_assets_account_asset_ledger", "account_assets", type_="unique")
    op.drop_constraint("uq_ledgers_id_owner", "ledgers", type_="unique")
