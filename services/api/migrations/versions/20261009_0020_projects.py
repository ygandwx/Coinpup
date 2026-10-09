"""Owned versioned project identities with transactional change notifications."""

import sqlalchemy as sa
from alembic import op

revision = "20261009_0020"
down_revision = "20261009_0019"
branch_labels = None
depends_on = None

_OLD_ENTITY_TYPES = (
    "'entities', 'ledgers', 'accounts', 'account_assets', 'categories', 'assets', "
    "'financial_operations', 'stored_files', 'operation_file_links', 'ocr_jobs', "
    "'ocr_drafts', 'ocr_confirmations', 'business_parties', 'business_documents', "
    "'business_document_lines', 'ledger_periods', 'ledger_period_audits', 'ledger_period_receipts'"
)
_ENTITY_TYPES = _OLD_ENTITY_TYPES + ", 'business_projects'"
_CHANGE_TRIGGER_SQL = {
    "business_projects": "CREATE TRIGGER trg_business_projects_change_log "
    "AFTER INSERT OR UPDATE ON business_projects FOR EACH ROW "
    "EXECUTE FUNCTION coinpup_record_change()"
}
_GUARD_SQL = """
    CREATE FUNCTION coinpup_guard_project() RETURNS trigger
    LANGUAGE plpgsql SET search_path = pg_catalog AS $$
    BEGIN
        IF TG_OP = 'INSERT' THEN
            IF NEW.version = 1 AND NOT NEW.archived THEN RETURN NEW; END IF;
        ELSIF TG_OP = 'UPDATE' THEN
            IF NEW.version::bigint = OLD.version::bigint + 1
               AND (to_jsonb(NEW) - ARRAY['version', 'archived', 'updated_at', 'name', 'notes'])
                   IS NOT DISTINCT FROM
                   (to_jsonb(OLD) - ARRAY['version', 'archived', 'updated_at', 'name', 'notes'])
            THEN
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
    op.create_table(
        "business_projects",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("ledger_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(160), nullable=False),
        sa.Column("notes", sa.String(2000), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("archived", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint("id", "ledger_id", name="uq_business_projects_id_ledger"),
        sa.ForeignKeyConstraint(
            ["ledger_id"], ["ledgers.id"], name="fk_business_projects_ledger", ondelete="RESTRICT"
        ),
        sa.CheckConstraint("version > 0", name="ck_business_projects_version"),
        sa.CheckConstraint("btrim(name) <> ''", name="ck_business_projects_name"),
    )
    op.create_index("ix_business_projects_ledger_id", "business_projects", ["ledger_id"])
    op.execute(_GUARD_SQL)
    op.execute(
        "CREATE TRIGGER tr_business_projects_identity BEFORE INSERT OR UPDATE OR DELETE "
        "ON business_projects FOR EACH ROW EXECUTE FUNCTION coinpup_guard_project()"
    )
    op.drop_constraint("ck_change_log_entity_type", "change_log", type_="check")
    op.create_check_constraint(
        "ck_change_log_entity_type", "change_log", f"entity_type IN ({_ENTITY_TYPES})"
    )
    op.execute(_CHANGE_TRIGGER_SQL["business_projects"])


def downgrade():
    op.execute("SELECT pg_advisory_xact_lock(18945999704708432)")
    op.execute("LOCK TABLE business_projects IN ACCESS EXCLUSIVE MODE")
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM public.business_projects)
               OR EXISTS (SELECT 1 FROM public.change_log WHERE entity_type = 'business_projects')
            THEN
                RAISE EXCEPTION USING ERRCODE = '23514',
                    MESSAGE = 'Project history prevents downgrade',
                    CONSTRAINT = 'ck_project_downgrade';
            END IF;
        END; $$
    """)
    op.drop_table("business_projects")
    op.execute("DROP FUNCTION coinpup_guard_project()")
    op.drop_constraint("ck_change_log_entity_type", "change_log", type_="check")
    op.create_check_constraint(
        "ck_change_log_entity_type", "change_log", f"entity_type IN ({_OLD_ENTITY_TYPES})"
    )
