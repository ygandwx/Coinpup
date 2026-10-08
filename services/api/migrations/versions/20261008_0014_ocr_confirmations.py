"""Preserve atomic OCR confirmation receipts and the final reviewed draft."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "20261008_0014"
down_revision = "20261004_0013"
branch_labels = None
depends_on = None

_OLD_ENTITY_TYPES = (
    "'entities', 'ledgers', 'accounts', 'account_assets', 'categories', 'assets', "
    "'financial_operations', 'stored_files', 'operation_file_links', 'ocr_jobs', 'ocr_drafts'"
)
_CHANGE_TRIGGER_SQL = {
    "ocr_confirmations": "CREATE TRIGGER trg_ocr_confirmations_change_log "
    "AFTER INSERT OR UPDATE ON ocr_confirmations "
    "FOR EACH ROW EXECUTE FUNCTION coinpup_record_change()"
}

# Frozen 0013 body, kept here so downgrade never imports mutable application/migration code.
_OLD_DRAFT_GUARD_SQL = """
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
_GUARD_SQL = """
    CREATE FUNCTION coinpup_guard_ocr_confirmation() RETURNS trigger
    LANGUAGE plpgsql SET search_path = pg_catalog AS $$
    BEGIN
        IF TG_OP = 'INSERT' THEN
            IF NEW.version IS NOT DISTINCT FROM 1 THEN RETURN NEW; END IF;
        END IF;
        RAISE EXCEPTION USING ERRCODE = '23514',
            MESSAGE = 'OCR confirmation history is immutable',
            CONSTRAINT = 'ck_ocr_confirmations_immutable';
    END;
    $$
"""
_CONSISTENCY_SQL = """
    CREATE FUNCTION coinpup_check_ocr_confirmation() RETURNS trigger
    LANGUAGE plpgsql SET search_path = pg_catalog AS $$
    DECLARE draft_key uuid; d record; c record;
    BEGIN
        IF TG_TABLE_NAME = 'ocr_drafts' THEN draft_key := NEW.id;
        ELSE draft_key := NEW.draft_id; END IF;
        SELECT * INTO d FROM public.ocr_drafts WHERE id = draft_key;
        SELECT * INTO c FROM public.ocr_confirmations WHERE draft_id = draft_key;
        IF d.id IS NULL OR (d.status = 'confirmed') IS DISTINCT FROM (c.id IS NOT NULL) THEN
            RAISE EXCEPTION USING ERRCODE = '23514',
                MESSAGE = 'OCR confirmation and draft must commit together',
                CONSTRAINT = 'ck_ocr_confirmation_consistency';
        END IF;
        IF c.id IS NULL THEN RETURN NULL; END IF;
        IF d.version <> c.draft_version::bigint + 1 OR d.fields IS DISTINCT FROM c.review
           OR d.source_key <> c.source_key OR NOT EXISTS (
            SELECT 1 FROM public.ocr_jobs j
            JOIN public.stored_files original ON original.id = j.file_id
                AND original.ledger_id = d.ledger_id
            JOIN public.stored_files target ON target.id = c.target_file_id
                AND target.ledger_id = c.target_ledger_id
            JOIN public.financial_operations o ON o.id = c.operation_id
                AND o.ledger_id = c.target_ledger_id AND o.version = c.operation_version
                AND o.status = 'active'
            JOIN public.operation_file_links evidence ON evidence.operation_id = o.id
                AND evidence.file_id = target.id AND evidence.ledger_id = c.target_ledger_id
                AND NOT evidence.archived
            WHERE j.id = d.job_id AND j.state = 'succeeded' AND original.id = c.source_file_id
                AND original.sha256 = target.sha256 AND original.byte_size = target.byte_size
                AND original.detected_media_type = target.detected_media_type
                AND NOT original.archived AND NOT target.archived
        ) THEN
            RAISE EXCEPTION USING ERRCODE = '23514',
                MESSAGE = 'OCR confirmation evidence or version is inconsistent',
                CONSTRAINT = 'ck_ocr_confirmation_consistency';
        END IF;
        RETURN NULL;
    END;
    $$
"""


def upgrade():
    op.execute("SELECT pg_advisory_xact_lock(18945999704708432)")
    op.create_table(
        "ocr_confirmations",
        sa.Column("id", sa.Uuid(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        *(
            sa.Column(name, sa.Uuid(), nullable=False)
            for name in (
                "ledger_id",
                "created_by",
                "draft_id",
                "intent_id",
                "source_file_id",
                "target_ledger_id",
                "target_file_id",
                "operation_id",
            )
        ),
        sa.Column("source_key", sa.String(200), nullable=False),
        sa.Column("action", sa.String(16), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("hash_version", sa.Integer(), nullable=False, server_default="2"),
        sa.Column("draft_version", sa.Integer(), nullable=False),
        sa.Column("operation_version", sa.Integer(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        *(
            sa.Column(
                name, sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
            )
            for name in ("created_at", "updated_at")
        ),
        *(sa.Column(name, postgresql.JSONB(), nullable=False) for name in ("review", "response")),
        sa.ForeignKeyConstraint(
            ["ledger_id", "created_by"],
            ["ledgers.id", "ledgers.owner_id"],
            name="fk_ocr_confirmations_ledger_owner",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["draft_id", "ledger_id", "created_by"],
            ["ocr_drafts.id", "ocr_drafts.ledger_id", "ocr_drafts.created_by"],
            name="fk_ocr_confirmations_draft_ledger_owner",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["target_ledger_id", "created_by"],
            ["ledgers.id", "ledgers.owner_id"],
            name="fk_ocr_confirmations_target_owner",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["source_file_id", "ledger_id"],
            ["stored_files.id", "stored_files.ledger_id"],
            name="fk_ocr_confirmations_source_file",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["target_file_id", "target_ledger_id"],
            ["stored_files.id", "stored_files.ledger_id"],
            name="fk_ocr_confirmations_target_file",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["operation_id", "target_ledger_id"],
            ["financial_operations.id", "financial_operations.ledger_id"],
            name="fk_ocr_confirmations_operation",
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("id", "ledger_id", name="uq_ocr_confirmations_id_ledger"),
        sa.UniqueConstraint(
            "id", "ledger_id", "created_by", name="uq_ocr_confirmations_id_ledger_owner"
        ),
        sa.UniqueConstraint("draft_id", name="uq_ocr_confirmations_draft"),
        sa.UniqueConstraint("ledger_id", "intent_id", name="uq_ocr_confirmations_ledger_intent"),
        sa.CheckConstraint("version > 0", name="ck_ocr_confirmations_version"),
        sa.CheckConstraint("updated_at >= created_at", name="ck_ocr_confirmations_timestamps"),
        sa.CheckConstraint("hash_version = 2", name="ck_ocr_confirmations_hash_version"),
        sa.CheckConstraint("request_hash ~ '^[0-9a-f]{64}$'", name="ck_ocr_confirmations_hash"),
        sa.CheckConstraint(
            "draft_version > 0 AND operation_version > 0", name="ck_ocr_confirmations_versions"
        ),
        sa.CheckConstraint("action IN ('create', 'link')", name="ck_ocr_confirmations_action"),
        *(
            sa.CheckConstraint(
                f"jsonb_typeof({name}) = 'object' AND octet_length({name}::text) <= 262144",
                name=f"ck_ocr_confirmations_{name}",
            )
            for name in ("review", "response")
        ),
    )
    op.create_index(
        "ix_ocr_confirmations_source",
        "ocr_confirmations",
        ["ledger_id", "source_file_id", "source_key"],
    )
    op.drop_constraint("ck_ocr_drafts_status", "ocr_drafts", type_="check")
    op.create_check_constraint(
        "ck_ocr_drafts_status", "ocr_drafts", "status IN ('draft', 'ignored', 'confirmed')"
    )
    op.execute(
        _OLD_DRAFT_GUARD_SQL.replace("CREATE FUNCTION", "CREATE OR REPLACE FUNCTION").replace(
            "ELSIF NEW.version", "ELSIF OLD.status = 'confirmed' OR NEW.version"
        )
    )
    op.execute(_GUARD_SQL)
    op.execute(_CONSISTENCY_SQL)
    op.execute(
        "CREATE TRIGGER tr_ocr_confirmations_guard BEFORE INSERT OR UPDATE OR DELETE "
        "ON ocr_confirmations FOR EACH ROW EXECUTE FUNCTION coinpup_guard_ocr_confirmation()"
    )
    for table in ("ocr_confirmations", "ocr_drafts"):
        op.execute(
            f"CREATE CONSTRAINT TRIGGER tr_{table}_consistency AFTER INSERT OR UPDATE "
            f"ON {table} DEFERRABLE INITIALLY DEFERRED FOR EACH ROW "
            "EXECUTE FUNCTION coinpup_check_ocr_confirmation()"
        )
    op.drop_constraint("ck_change_log_entity_type", "change_log", type_="check")
    op.create_check_constraint(
        "ck_change_log_entity_type",
        "change_log",
        f"entity_type IN ({_OLD_ENTITY_TYPES}, 'ocr_confirmations')",
    )
    op.execute(_CHANGE_TRIGGER_SQL["ocr_confirmations"])


def downgrade():
    op.execute("SELECT pg_advisory_xact_lock(18945999704708432)")
    op.execute("LOCK TABLE ocr_confirmations, ocr_drafts IN ACCESS EXCLUSIVE MODE")
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM public.ocr_confirmations)
               OR EXISTS (SELECT 1 FROM public.ocr_drafts WHERE status = 'confirmed')
               OR EXISTS (SELECT 1 FROM public.change_log
                          WHERE entity_type = 'ocr_confirmations') THEN
                RAISE EXCEPTION USING ERRCODE = '23514',
                    MESSAGE = 'OCR confirmation history prevents downgrade',
                    CONSTRAINT = 'ck_ocr_confirmation_downgrade';
            END IF;
        END; $$
    """)
    op.execute("DROP TRIGGER tr_ocr_drafts_consistency ON ocr_drafts")
    op.drop_table("ocr_confirmations")
    op.execute("DROP FUNCTION coinpup_check_ocr_confirmation()")
    op.execute("DROP FUNCTION coinpup_guard_ocr_confirmation()")
    op.execute(_OLD_DRAFT_GUARD_SQL.replace("CREATE FUNCTION", "CREATE OR REPLACE FUNCTION"))
    op.drop_constraint("ck_ocr_drafts_status", "ocr_drafts", type_="check")
    op.create_check_constraint(
        "ck_ocr_drafts_status", "ocr_drafts", "status IN ('draft', 'ignored')"
    )
    op.drop_constraint("ck_change_log_entity_type", "change_log", type_="check")
    op.create_check_constraint(
        "ck_change_log_entity_type", "change_log", f"entity_type IN ({_OLD_ENTITY_TYPES})"
    )
