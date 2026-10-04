"""Append owner-scoped changes in transaction commit order."""

import sqlalchemy as sa
from alembic import op

revision = "20261004_0012"
down_revision = "20261004_0011"
branch_labels = None
depends_on = None

# Frozen with this migration; no runtime application imports or table discovery.
_TRACKED_TABLES = (
    "entities",
    "ledgers",
    "accounts",
    "account_assets",
    "categories",
    "assets",
    "financial_operations",
    "stored_files",
    "operation_file_links",
)
_CHANGE_TRIGGER_SQL = {
    table: f"CREATE TRIGGER trg_{table}_change_log AFTER INSERT OR UPDATE ON {table} "
    "FOR EACH ROW EXECUTE FUNCTION coinpup_record_change()"
    for table in _TRACKED_TABLES
}

_GUARD_SQL = """
    CREATE FUNCTION coinpup_guard_change_log() RETURNS trigger
    LANGUAGE plpgsql SET search_path = pg_catalog AS $$
    BEGIN
        IF TG_OP <> 'INSERT' THEN
            RAISE EXCEPTION USING ERRCODE = '23514',
                MESSAGE = 'Change log records are immutable',
                CONSTRAINT = 'ck_change_log_immutable';
        END IF;
        IF pg_trigger_depth() < 2 THEN
            RAISE EXCEPTION USING ERRCODE = '23514',
                MESSAGE = 'Change log records require a business trigger',
                CONSTRAINT = 'ck_change_log_source';
        END IF;
        RETURN NEW;
    END;
    $$
"""

_RECORD_SQL = """
    CREATE FUNCTION coinpup_record_change() RETURNS trigger
    LANGUAGE plpgsql SET search_path = pg_catalog AS $$
    DECLARE new_data jsonb; old_data jsonb;
            change_owner uuid; change_ledger uuid; object_id text;
            object_version integer; operation_kind text := 'upsert';
    BEGIN
        -- Lock before the INSERT evaluates its identity default. Locking in the
        -- change_log BEFORE trigger would happen after sequence allocation.
        PERFORM pg_advisory_xact_lock(18945999704708432);
        new_data := to_jsonb(NEW);
        IF TG_OP = 'UPDATE' THEN
            old_data := to_jsonb(OLD);
        END IF;

        IF TG_TABLE_NAME = 'assets' THEN
            SELECT id INTO change_owner FROM public.administrators WHERE singleton = 1;
            -- Built-in assets predate administrator setup. Full reads include them.
            IF NOT FOUND THEN
                RETURN NEW;
            END IF;
            object_id := new_data->>'asset_id';
        ELSIF TG_TABLE_NAME = 'entities' THEN
            change_owner := (new_data->>'owner_id')::uuid;
            object_id := new_data->>'id';
        ELSIF TG_TABLE_NAME = 'ledgers' THEN
            change_owner := (new_data->>'owner_id')::uuid;
            change_ledger := (new_data->>'id')::uuid;
            object_id := new_data->>'id';
        ELSE
            change_ledger := (new_data->>'ledger_id')::uuid;
            SELECT owner_id INTO change_owner FROM public.ledgers WHERE id = change_ledger;
            IF TG_TABLE_NAME = 'account_assets' THEN
                object_id := (new_data->>'account_id') || ':' || (new_data->>'asset_id');
                SELECT version INTO object_version FROM public.accounts
                WHERE id = (new_data->>'account_id')::uuid AND ledger_id = change_ledger;
            ELSE
                object_id := new_data->>'id';
            END IF;
        END IF;
        IF TG_TABLE_NAME <> 'account_assets' THEN
            object_version := (new_data->>'version')::integer;
        END IF;

        IF TG_OP = 'UPDATE' THEN
            IF TG_TABLE_NAME = 'financial_operations'
               AND new_data->>'status' = 'cancelled'
               AND old_data->>'status' IS DISTINCT FROM 'cancelled' THEN
                operation_kind := 'cancel';
            ELSIF new_data ? 'archived'
                  AND new_data->>'archived' IS DISTINCT FROM old_data->>'archived' THEN
                operation_kind := CASE WHEN (new_data->>'archived')::boolean
                    THEN 'archive' ELSE 'restore' END;
            ELSIF new_data ? 'enabled'
                  AND new_data->>'enabled' IS DISTINCT FROM old_data->>'enabled' THEN
                operation_kind := CASE WHEN (new_data->>'enabled')::boolean
                    THEN 'restore' ELSE 'archive' END;
            END IF;
        END IF;

        INSERT INTO public.change_log (
            owner_id, ledger_id, entity_type, entity_id, entity_version, change_kind
        ) VALUES (
            change_owner, change_ledger, TG_TABLE_NAME, object_id, object_version, operation_kind
        );
        RETURN NEW;
    END;
    $$
"""


def upgrade() -> None:
    op.create_table(
        "change_log",
        sa.Column("seq", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("owner_id", sa.Uuid(), nullable=False),
        sa.Column("ledger_id", sa.Uuid(), nullable=True),
        sa.Column("entity_type", sa.String(32), nullable=False),
        sa.Column("entity_id", sa.Text(), nullable=False),
        sa.Column("entity_version", sa.Integer(), nullable=False),
        sa.Column("change_kind", sa.String(8), nullable=False),
        sa.Column(
            "changed_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.clock_timestamp(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("seq"),
        sa.ForeignKeyConstraint(
            ["owner_id"],
            ["administrators.id"],
            name="fk_change_log_owner",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["ledger_id", "owner_id"],
            ["ledgers.id", "ledgers.owner_id"],
            name="fk_change_log_ledger_owner",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint("seq > 0", name="ck_change_log_seq"),
        sa.CheckConstraint("entity_version > 0", name="ck_change_log_entity_version"),
        sa.CheckConstraint("btrim(entity_id) <> ''", name="ck_change_log_entity_id"),
        sa.CheckConstraint(
            "entity_type IN ('entities', 'ledgers', 'accounts', 'account_assets', "
            "'categories', 'assets', 'financial_operations', 'stored_files', "
            "'operation_file_links')",
            name="ck_change_log_entity_type",
        ),
        sa.CheckConstraint(
            "change_kind IN ('upsert', 'archive', 'restore', 'cancel')",
            name="ck_change_log_change_kind",
        ),
    )
    op.create_index("ix_change_log_owner_seq", "change_log", ["owner_id", "seq"])
    op.execute(_GUARD_SQL)
    op.execute(
        "CREATE TRIGGER trg_change_log_guard BEFORE INSERT OR UPDATE OR DELETE ON change_log "
        "FOR EACH ROW EXECUTE FUNCTION coinpup_guard_change_log()"
    )
    op.execute(_RECORD_SQL)
    for statement in _CHANGE_TRIGGER_SQL.values():
        op.execute(statement)


def downgrade() -> None:
    op.execute("SELECT pg_advisory_xact_lock(18945999704708432)")
    op.execute("""
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM public.change_log) THEN
                RAISE EXCEPTION USING ERRCODE = '23514',
                    MESSAGE = 'Change log history prevents downgrade',
                    CONSTRAINT = 'ck_change_log_downgrade';
            END IF;
        END;
        $$
    """)
    for table in reversed(_TRACKED_TABLES):
        op.execute(f"DROP TRIGGER trg_{table}_change_log ON {table}")
    op.execute("DROP FUNCTION coinpup_record_change()")
    op.execute("DROP TRIGGER trg_change_log_guard ON change_log")
    op.execute("DROP FUNCTION coinpup_guard_change_log()")
    op.drop_index("ix_change_log_owner_seq", table_name="change_log")
    op.drop_table("change_log")
