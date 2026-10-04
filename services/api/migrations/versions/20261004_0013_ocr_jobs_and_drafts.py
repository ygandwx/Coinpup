"""Persist bounded OCR jobs and editable candidates without financial side effects."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "20261004_0013"
down_revision = "20261004_0012"
branch_labels = None
depends_on = None

# Frozen registration for this increment; the generic 0012 recorder is unchanged.
_TRACKED_TABLES = ("ocr_jobs", "ocr_drafts")
_CHANGE_TRIGGER_SQL = {
    table: f"CREATE TRIGGER trg_{table}_change_log AFTER INSERT OR UPDATE ON {table} "
    "FOR EACH ROW EXECUTE FUNCTION coinpup_record_change()"
    for table in _TRACKED_TABLES
}
_OLD_ENTITY_TYPES = (
    "'entities', 'ledgers', 'accounts', 'account_assets', 'categories', 'assets', "
    "'financial_operations', 'stored_files', 'operation_file_links'"
)

_JOB_GUARD_SQL = """
    CREATE FUNCTION coinpup_guard_ocr_job() RETURNS trigger
    LANGUAGE plpgsql SET search_path = pg_catalog AS $$
    BEGIN
        IF TG_OP = 'DELETE' THEN
            RAISE EXCEPTION USING ERRCODE = '23514',
                MESSAGE = 'OCR job history is immutable', CONSTRAINT = 'ck_ocr_jobs_immutable';
        ELSIF TG_OP = 'INSERT' THEN
            IF NEW.version IS DISTINCT FROM 1 OR NEW.state IS DISTINCT FROM 'pending'
               OR NEW.attempts IS DISTINCT FROM 0 OR NEW.generation IS DISTINCT FROM 0
               OR NEW.lease_token IS NOT NULL OR NEW.lease_until IS NOT NULL
               OR NEW.result IS NOT NULL OR NEW.error_code IS NOT NULL THEN
                RAISE EXCEPTION USING ERRCODE = '23514',
                    MESSAGE = 'OCR jobs must start pending', CONSTRAINT = 'ck_ocr_jobs_immutable';
            END IF;
        ELSIF OLD.state = 'succeeded' OR NEW.version IS DISTINCT FROM OLD.version + 1
           OR NEW.attempts < OLD.attempts OR NEW.generation < OLD.generation
           OR (to_jsonb(NEW) - ARRAY['state', 'attempts', 'generation', 'lease_token',
                'lease_until', 'retry_at', 'error_code', 'result', 'version', 'updated_at'])
              IS DISTINCT FROM
              (to_jsonb(OLD) - ARRAY['state', 'attempts', 'generation', 'lease_token',
                'lease_until', 'retry_at', 'error_code', 'result', 'version', 'updated_at']) THEN
            RAISE EXCEPTION USING ERRCODE = '23514',
                MESSAGE = 'OCR job identity or version is invalid',
                CONSTRAINT = 'ck_ocr_jobs_immutable';
        END IF;
        RETURN NEW;
    END;
    $$
"""

_DRAFT_GUARD_SQL = """
    CREATE FUNCTION coinpup_guard_ocr_draft() RETURNS trigger
    LANGUAGE plpgsql SET search_path = pg_catalog AS $$
    BEGIN
        IF TG_OP = 'DELETE' THEN
            RAISE EXCEPTION USING ERRCODE = '23514',
                MESSAGE = 'OCR draft history is immutable', CONSTRAINT = 'ck_ocr_drafts_immutable';
        ELSIF TG_OP = 'INSERT' THEN
            IF NEW.version IS DISTINCT FROM 1 OR NEW.status IS DISTINCT FROM 'draft' THEN
                RAISE EXCEPTION USING ERRCODE = '23514',
                    MESSAGE = 'OCR drafts must start editable',
                    CONSTRAINT = 'ck_ocr_drafts_immutable';
            END IF;
        ELSIF NEW.version IS DISTINCT FROM OLD.version + 1
           OR (to_jsonb(NEW) - ARRAY['fields', 'status', 'version', 'updated_at'])
              IS DISTINCT FROM
              (to_jsonb(OLD) - ARRAY['fields', 'status', 'version', 'updated_at']) THEN
            RAISE EXCEPTION USING ERRCODE = '23514',
                MESSAGE = 'OCR draft identity or version is invalid',
                CONSTRAINT = 'ck_ocr_drafts_immutable';
        END IF;
        RETURN NEW;
    END;
    $$
"""


def _columns():
    return (
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("ledger_id", sa.Uuid(), nullable=False),
        sa.Column("created_by", sa.Uuid(), nullable=False),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )


def _scope(table):
    return (
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(
            ["ledger_id", "created_by"],
            ["ledgers.id", "ledgers.owner_id"],
            name=f"fk_{table}_ledger_owner",
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("id", "ledger_id", name=f"uq_{table}_id_ledger"),
        sa.UniqueConstraint("id", "ledger_id", "created_by", name=f"uq_{table}_id_ledger_owner"),
        sa.CheckConstraint("version > 0", name=f"ck_{table}_version"),
        sa.CheckConstraint("updated_at >= created_at", name=f"ck_{table}_timestamps"),
    )


def upgrade() -> None:
    # Match business writers before changing the shared log's type constraint.
    op.execute("SELECT pg_advisory_xact_lock(18945999704708432)")
    op.create_table(
        "ocr_jobs",
        *_columns(),
        sa.Column("file_id", sa.Uuid(), nullable=False),
        sa.Column("intent_id", sa.Uuid(), nullable=False),
        sa.Column("manifest_hash", sa.String(64), nullable=False),
        sa.Column("configuration", postgresql.JSONB(), nullable=False),
        sa.Column("config_hash", sa.String(64), nullable=False),
        sa.Column("state", sa.String(16), server_default="pending", nullable=False),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("generation", sa.Integer(), server_default="0", nullable=False),
        sa.Column("lease_token", sa.Uuid(), nullable=True),
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "retry_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("error_code", sa.String(64), nullable=True),
        sa.Column("result", postgresql.JSONB(none_as_null=True), nullable=True),
        *_scope("ocr_jobs"),
        sa.ForeignKeyConstraint(
            ["file_id", "ledger_id"],
            ["stored_files.id", "stored_files.ledger_id"],
            name="fk_ocr_jobs_file_ledger",
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("created_by", "intent_id", name="uq_ocr_jobs_owner_intent"),
        sa.CheckConstraint("manifest_hash ~ '^[0-9a-f]{64}$'", name="ck_ocr_jobs_manifest_hash"),
        sa.CheckConstraint("config_hash ~ '^[0-9a-f]{64}$'", name="ck_ocr_jobs_config_hash"),
        sa.CheckConstraint(
            "jsonb_typeof(configuration) = 'object' AND octet_length(configuration::text) <= 16384",
            name="ck_ocr_jobs_configuration",
        ),
        sa.CheckConstraint(
            "result IS NULL OR (jsonb_typeof(result) = 'object' "
            "AND octet_length(result::text) <= 1048576)",
            name="ck_ocr_jobs_result",
        ),
        sa.CheckConstraint(
            "state IN ('pending', 'running', 'succeeded', 'failed')",
            name="ck_ocr_jobs_state",
        ),
        sa.CheckConstraint("attempts BETWEEN 0 AND 3", name="ck_ocr_jobs_attempts"),
        sa.CheckConstraint("generation >= 0", name="ck_ocr_jobs_generation"),
        sa.CheckConstraint(
            "(state = 'running' AND lease_token IS NOT NULL AND lease_until IS NOT NULL "
            "AND attempts >= 1 AND generation >= 1) OR "
            "(state <> 'running' AND lease_token IS NULL AND lease_until IS NULL)",
            name="ck_ocr_jobs_lease",
        ),
        sa.CheckConstraint(
            "error_code IS NULL OR error_code ~ '^[a-z][a-z0-9_]{0,63}$'",
            name="ck_ocr_jobs_error_code",
        ),
        sa.CheckConstraint(
            "(state <> 'succeeded' OR (result IS NOT NULL AND error_code IS NULL)) "
            "AND (state <> 'failed' OR error_code IS NOT NULL)",
            name="ck_ocr_jobs_completion",
        ),
    )
    op.create_index("ix_ocr_jobs_ready", "ocr_jobs", ["state", "retry_at"])
    op.create_index("ix_ocr_jobs_ledger_id", "ocr_jobs", ["ledger_id"])
    op.create_table(
        "ocr_drafts",
        *_columns(),
        sa.Column("job_id", sa.Uuid(), nullable=False),
        sa.Column("source_key", sa.String(200), nullable=False),
        sa.Column("recognized", postgresql.JSONB(), nullable=False),
        sa.Column("evidence", postgresql.JSONB(), nullable=False),
        sa.Column("fields", postgresql.JSONB(), nullable=False),
        sa.Column("status", sa.String(16), server_default="draft", nullable=False),
        *_scope("ocr_drafts"),
        sa.ForeignKeyConstraint(
            ["job_id", "ledger_id", "created_by"],
            ["ocr_jobs.id", "ocr_jobs.ledger_id", "ocr_jobs.created_by"],
            name="fk_ocr_drafts_job_ledger_owner",
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("job_id", "source_key", name="uq_ocr_drafts_job_source"),
        sa.CheckConstraint(
            "source_key ~ '[^[:space:]]' AND source_key !~ '[[:cntrl:]]'",
            name="ck_ocr_drafts_source_key",
        ),
        *(
            sa.CheckConstraint(
                f"jsonb_typeof({column}) = 'object' AND octet_length({column}::text) <= 262144",
                name=f"ck_ocr_drafts_{column}",
            )
            for column in ("recognized", "evidence", "fields")
        ),
        sa.CheckConstraint("status IN ('draft', 'ignored')", name="ck_ocr_drafts_status"),
    )
    op.create_index("ix_ocr_drafts_ledger_id", "ocr_drafts", ["ledger_id"])
    op.execute(_JOB_GUARD_SQL)
    op.execute(_DRAFT_GUARD_SQL)
    for table, function in (
        ("ocr_jobs", "coinpup_guard_ocr_job"),
        ("ocr_drafts", "coinpup_guard_ocr_draft"),
    ):
        op.execute(
            f"CREATE TRIGGER trg_{table}_guard BEFORE INSERT OR UPDATE OR DELETE ON {table} "
            f"FOR EACH ROW EXECUTE FUNCTION {function}()"
        )
    op.drop_constraint("ck_change_log_entity_type", "change_log", type_="check")
    op.create_check_constraint(
        "ck_change_log_entity_type",
        "change_log",
        f"entity_type IN ({_OLD_ENTITY_TYPES}, 'ocr_jobs', 'ocr_drafts')",
    )
    for statement in _CHANGE_TRIGGER_SQL.values():
        op.execute(statement)


def downgrade() -> None:
    op.execute("SELECT pg_advisory_xact_lock(18945999704708432)")
    op.execute("LOCK TABLE ocr_jobs, ocr_drafts IN ACCESS EXCLUSIVE MODE")
    op.execute("""
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM public.ocr_jobs)
               OR EXISTS (SELECT 1 FROM public.ocr_drafts)
               OR EXISTS (SELECT 1 FROM public.change_log
                          WHERE entity_type IN ('ocr_jobs', 'ocr_drafts')) THEN
                RAISE EXCEPTION USING ERRCODE = '23514',
                    MESSAGE = 'OCR history prevents downgrade', CONSTRAINT = 'ck_ocr_downgrade';
            END IF;
        END;
        $$
    """)
    for table in reversed(_TRACKED_TABLES):
        op.execute(f"DROP TRIGGER trg_{table}_change_log ON {table}")
        op.execute(f"DROP TRIGGER trg_{table}_guard ON {table}")
    op.execute("DROP FUNCTION coinpup_guard_ocr_draft()")
    op.execute("DROP FUNCTION coinpup_guard_ocr_job()")
    op.drop_table("ocr_drafts")
    op.drop_table("ocr_jobs")
    op.drop_constraint("ck_change_log_entity_type", "change_log", type_="check")
    op.create_check_constraint(
        "ck_change_log_entity_type",
        "change_log",
        f"entity_type IN ({_OLD_ENTITY_TYPES})",
    )
