"""Scoped reference identities without T06 business commands or document amounts."""

import sqlalchemy as sa
from alembic import op

revision = "20261009_0016"
down_revision = "20261009_0015"
branch_labels = None
depends_on = None

_TABLES = ("business_parties", "business_documents", "business_document_lines")
_OLD_ENTITY_TYPES = (
    "'entities', 'ledgers', 'accounts', 'account_assets', 'categories', 'assets', "
    "'financial_operations', 'stored_files', 'operation_file_links', "
    "'ocr_jobs', 'ocr_drafts', 'ocr_confirmations'"
)
_ENTITY_TYPES = _OLD_ENTITY_TYPES + ", " + ", ".join(f"'{table}'" for table in _TABLES)
_CHANGE_TRIGGER_SQL = {
    table: f"CREATE TRIGGER trg_{table}_change_log AFTER INSERT OR UPDATE ON {table} "
    "FOR EACH ROW EXECUTE FUNCTION coinpup_record_change()"
    for table in _TABLES
}
_GUARD_SQL = """
    CREATE FUNCTION coinpup_guard_business_reference() RETURNS trigger
    LANGUAGE plpgsql SET search_path = pg_catalog AS $$
    BEGIN
        IF TG_OP = 'INSERT' THEN
            IF NEW.version = 1 AND NOT NEW.archived THEN RETURN NEW; END IF;
        ELSIF TG_OP = 'UPDATE' THEN
            IF NEW.version::bigint = OLD.version::bigint + 1
               AND (to_jsonb(NEW) - ARRAY['version', 'archived', 'updated_at'])
                   IS NOT DISTINCT FROM
                   (to_jsonb(OLD) - ARRAY['version', 'archived', 'updated_at']) THEN
                RETURN NEW;
            END IF;
        END IF;
        RAISE EXCEPTION USING ERRCODE = '23514',
            MESSAGE = 'Business reference identity or version is immutable',
            CONSTRAINT = 'ck_business_reference_identity';
    END; $$
"""


def upgrade():
    op.execute("SELECT pg_advisory_xact_lock(18945999704708432)")
    for table in _TABLES:
        extra = []
        if table == "business_document_lines":
            extra = [
                sa.Column("document_id", sa.Uuid(), nullable=False),
                sa.UniqueConstraint(
                    "id",
                    "document_id",
                    "ledger_id",
                    name="uq_business_document_lines_document_ledger",
                ),
                sa.ForeignKeyConstraint(
                    ["document_id", "ledger_id"],
                    ["business_documents.id", "business_documents.ledger_id"],
                    name="fk_business_document_lines_document_ledger",
                    ondelete="RESTRICT",
                ),
            ]
        op.create_table(
            table,
            sa.Column("id", sa.Uuid(), primary_key=True),
            sa.Column("ledger_id", sa.Uuid(), nullable=False),
            sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("archived", sa.Boolean(), nullable=False, server_default=sa.text("false")),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.func.now(),
            ),
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.func.now(),
            ),
            sa.UniqueConstraint("id", "ledger_id", name=f"uq_{table}_id_ledger"),
            sa.ForeignKeyConstraint(
                ["ledger_id"], ["ledgers.id"], name=f"fk_{table}_ledger", ondelete="RESTRICT"
            ),
            sa.CheckConstraint("version > 0", name=f"ck_{table}_version"),
            *extra,
        )
        op.create_index(f"ix_{table}_ledger_id", table, ["ledger_id"])
    op.execute(_GUARD_SQL)
    op.drop_constraint("ck_change_log_entity_type", "change_log", type_="check")
    op.create_check_constraint(
        "ck_change_log_entity_type", "change_log", f"entity_type IN ({_ENTITY_TYPES})"
    )
    for table in _TABLES:
        op.execute(
            f"CREATE TRIGGER tr_{table}_identity BEFORE INSERT OR UPDATE OR DELETE "
            f"ON {table} FOR EACH ROW EXECUTE FUNCTION coinpup_guard_business_reference()"
        )
        op.execute(_CHANGE_TRIGGER_SQL[table])


def downgrade():
    op.execute("SELECT pg_advisory_xact_lock(18945999704708432)")
    op.execute("LOCK TABLE " + ", ".join(_TABLES) + " IN ACCESS EXCLUSIVE MODE")
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM public.business_parties)
               OR EXISTS (SELECT 1 FROM public.business_documents)
               OR EXISTS (SELECT 1 FROM public.business_document_lines)
               OR EXISTS (SELECT 1 FROM public.change_log
                    WHERE entity_type IN ('business_parties', 'business_documents',
                                          'business_document_lines')) THEN
                RAISE EXCEPTION USING ERRCODE = '23514',
                    MESSAGE = 'Business reference history prevents downgrade',
                    CONSTRAINT = 'ck_business_reference_downgrade';
            END IF;
        END; $$
    """)
    for table in reversed(_TABLES):
        op.drop_table(table)
    op.execute("DROP FUNCTION coinpup_guard_business_reference()")
    op.drop_constraint("ck_change_log_entity_type", "change_log", type_="check")
    op.create_check_constraint(
        "ck_change_log_entity_type", "change_log", f"entity_type IN ({_OLD_ENTITY_TYPES})"
    )
