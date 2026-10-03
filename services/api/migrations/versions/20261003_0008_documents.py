"""Private document reservations, immutable content identity and operation links."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "20261003_0008"
down_revision = "20261003_0007"
branch_labels = None
depends_on = None


def _version_columns() -> list[sa.Column]:
    return [
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    ]


def upgrade() -> None:
    op.create_table(
        "stored_files",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("ledger_id", sa.Uuid(), nullable=False),
        sa.Column("created_by", sa.Uuid(), nullable=False),
        sa.Column("blob_key", sa.String(32), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("byte_size", sa.BigInteger(), nullable=False),
        sa.Column("detected_media_type", sa.String(64), nullable=False),
        sa.Column("original_filename", sa.String(255), nullable=False),
        sa.Column("title", sa.String(255), nullable=False),
        sa.Column("archived", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        *_version_columns(),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("id", "ledger_id", name="uq_stored_files_id_ledger"),
        sa.UniqueConstraint("blob_key", name="uq_stored_files_blob_key"),
        sa.UniqueConstraint(
            "ledger_id", "sha256", "byte_size", name="uq_stored_files_ledger_content"
        ),
        sa.ForeignKeyConstraint(
            ["ledger_id", "created_by"],
            ["ledgers.id", "ledgers.owner_id"],
            name="fk_stored_files_ledger_owner",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint("blob_key ~ '^[0-9a-f]{32}$'", name="ck_stored_files_blob_key"),
        sa.CheckConstraint("sha256 ~ '^[0-9a-f]{64}$'", name="ck_stored_files_sha256"),
        sa.CheckConstraint("byte_size > 0", name="ck_stored_files_byte_size"),
        sa.CheckConstraint(
            "detected_media_type IN ('application/pdf', 'image/jpeg', 'image/png', 'image/webp')",
            name="ck_stored_files_media_type",
        ),
        sa.CheckConstraint(
            "original_filename ~ '[^[:space:]]' AND original_filename !~ '[[:cntrl:]]'",
            name="ck_stored_files_filename",
        ),
        sa.CheckConstraint("title ~ '[^[:space:]]'", name="ck_stored_files_title"),
        sa.CheckConstraint("version > 0", name="ck_stored_files_version"),
        sa.CheckConstraint("updated_at >= created_at", name="ck_stored_files_timestamps"),
    )
    op.create_index("ix_stored_files_ledger_id", "stored_files", ["ledger_id"])
    op.create_table(
        "file_uploads",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("ledger_id", sa.Uuid(), nullable=False),
        sa.Column("created_by", sa.Uuid(), nullable=False),
        sa.Column("original_filename", sa.String(255), nullable=False),
        sa.Column("declared_size", sa.BigInteger(), nullable=False),
        sa.Column("operation_id", sa.Uuid(), nullable=True),
        sa.Column("manifest_hash", sa.String(64), nullable=False),
        sa.Column("state", sa.String(8), server_default="pending", nullable=False),
        sa.Column("file_id", sa.Uuid(), nullable=True),
        sa.Column("response", postgresql.JSONB(none_as_null=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("id", "ledger_id", name="uq_file_uploads_id_ledger"),
        sa.ForeignKeyConstraint(
            ["ledger_id", "created_by"],
            ["ledgers.id", "ledgers.owner_id"],
            name="fk_file_uploads_ledger_owner",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["operation_id", "ledger_id"],
            ["financial_operations.id", "financial_operations.ledger_id"],
            name="fk_file_uploads_operation_ledger",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["file_id", "ledger_id"],
            ["stored_files.id", "stored_files.ledger_id"],
            name="fk_file_uploads_file_ledger",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "original_filename ~ '[^[:space:]]' AND original_filename !~ '[[:cntrl:]]'",
            name="ck_file_uploads_filename",
        ),
        sa.CheckConstraint("declared_size > 0", name="ck_file_uploads_declared_size"),
        sa.CheckConstraint(
            "manifest_hash ~ '^[0-9a-f]{64}$'", name="ck_file_uploads_manifest_hash"
        ),
        sa.CheckConstraint(
            "(state = 'pending' AND file_id IS NULL AND response IS NULL "
            "AND completed_at IS NULL) OR "
            "(state = 'ready' AND file_id IS NOT NULL AND response IS NOT NULL "
            "AND completed_at IS NOT NULL)",
            name="ck_file_uploads_completion",
        ),
        sa.CheckConstraint(
            "response IS NULL OR jsonb_typeof(response) = 'object'", name="ck_file_uploads_response"
        ),
        sa.CheckConstraint(
            "completed_at IS NULL OR completed_at >= created_at", name="ck_file_uploads_timestamps"
        ),
    )
    for column in ("ledger_id", "operation_id", "file_id"):
        op.create_index(f"ix_file_uploads_{column}", "file_uploads", [column])
    op.create_table(
        "operation_file_links",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("file_id", sa.Uuid(), nullable=False),
        sa.Column("operation_id", sa.Uuid(), nullable=False),
        sa.Column("ledger_id", sa.Uuid(), nullable=False),
        sa.Column("created_by", sa.Uuid(), nullable=False),
        sa.Column("archived", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        *_version_columns(),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("id", "ledger_id", name="uq_operation_file_links_id_ledger"),
        sa.UniqueConstraint("file_id", "operation_id", name="uq_operation_file_links_pair"),
        sa.ForeignKeyConstraint(
            ["ledger_id", "created_by"],
            ["ledgers.id", "ledgers.owner_id"],
            name="fk_operation_file_links_ledger_owner",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["file_id", "ledger_id"],
            ["stored_files.id", "stored_files.ledger_id"],
            name="fk_operation_file_links_file_ledger",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["operation_id", "ledger_id"],
            ["financial_operations.id", "financial_operations.ledger_id"],
            name="fk_operation_file_links_operation_ledger",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint("version > 0", name="ck_operation_file_links_version"),
        sa.CheckConstraint("updated_at >= created_at", name="ck_operation_file_links_timestamps"),
    )
    for column in ("ledger_id", "operation_id", "file_id"):
        op.create_index(f"ix_operation_file_links_{column}", "operation_file_links", [column])

    op.execute("""
        CREATE FUNCTION coinpup_guard_stored_file() RETURNS trigger
        LANGUAGE plpgsql SET search_path = pg_catalog AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION USING ERRCODE = '23514',
                    MESSAGE = 'Stored files cannot be deleted',
                    CONSTRAINT = 'ck_stored_files_immutable';
            ELSIF TG_OP = 'INSERT' THEN
                IF NEW.version <> 1 OR NEW.archived THEN
                    RAISE EXCEPTION USING ERRCODE = '23514',
                        MESSAGE = 'Files start active at version one',
                        CONSTRAINT = 'ck_stored_files_immutable';
                END IF;
            ELSIF (to_jsonb(NEW) - ARRAY['title', 'archived', 'version', 'updated_at'])
                IS DISTINCT FROM
                (to_jsonb(OLD) - ARRAY['title', 'archived', 'version', 'updated_at'])
                OR NEW.version <> OLD.version + 1 THEN
                RAISE EXCEPTION USING ERRCODE = '23514',
                    MESSAGE = 'Content identity is immutable and metadata increments its version',
                    CONSTRAINT = 'ck_stored_files_immutable';
            END IF;
            RETURN NEW;
        END;
        $$
    """)
    op.execute("""
        CREATE TRIGGER trg_stored_files_guard BEFORE INSERT OR UPDATE OR DELETE ON stored_files
        FOR EACH ROW EXECUTE FUNCTION coinpup_guard_stored_file()
    """)
    op.execute("""
        CREATE FUNCTION coinpup_guard_file_upload() RETURNS trigger
        LANGUAGE plpgsql SET search_path = pg_catalog AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION USING ERRCODE = '23514',
                    MESSAGE = 'Upload reservations cannot be deleted',
                    CONSTRAINT = 'ck_file_uploads_immutable';
            ELSIF TG_OP = 'INSERT' THEN
                IF NEW.state <> 'pending' THEN
                    RAISE EXCEPTION USING ERRCODE = '23514',
                        MESSAGE = 'An upload begins pending',
                        CONSTRAINT = 'ck_file_uploads_immutable';
                END IF;
            ELSIF OLD.state <> 'pending' OR NEW.state <> 'ready' OR
                (to_jsonb(NEW) - ARRAY['state', 'file_id', 'response', 'completed_at'])
                IS DISTINCT FROM
                (to_jsonb(OLD) - ARRAY['state', 'file_id', 'response', 'completed_at']) THEN
                RAISE EXCEPTION USING ERRCODE = '23514',
                    MESSAGE = 'Only a single upload completion is permitted',
                    CONSTRAINT = 'ck_file_uploads_immutable';
            END IF;
            RETURN NEW;
        END;
        $$
    """)
    op.execute("""
        CREATE TRIGGER trg_file_uploads_guard BEFORE INSERT OR UPDATE OR DELETE ON file_uploads
        FOR EACH ROW EXECUTE FUNCTION coinpup_guard_file_upload()
    """)
    op.execute("""
        CREATE FUNCTION coinpup_guard_operation_file_link() RETURNS trigger
        LANGUAGE plpgsql SET search_path = pg_catalog AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION USING ERRCODE = '23514',
                    MESSAGE = 'File links cannot be deleted',
                    CONSTRAINT = 'ck_operation_file_links_immutable';
            ELSIF TG_OP = 'INSERT' THEN
                IF NEW.version <> 1 OR NEW.archived THEN
                    RAISE EXCEPTION USING ERRCODE = '23514',
                        MESSAGE = 'File links start active at version one',
                        CONSTRAINT = 'ck_operation_file_links_immutable';
                END IF;
            ELSIF (to_jsonb(NEW) - ARRAY['archived', 'version', 'updated_at']) IS DISTINCT FROM
                (to_jsonb(OLD) - ARRAY['archived', 'version', 'updated_at']) OR
                NEW.version <> OLD.version + 1 THEN
                RAISE EXCEPTION USING ERRCODE = '23514',
                    MESSAGE = 'Link identity is immutable; archive changes increment its version',
                    CONSTRAINT = 'ck_operation_file_links_immutable';
            END IF;
            RETURN NEW;
        END;
        $$
    """)
    op.execute("""
        CREATE TRIGGER trg_operation_file_links_guard
        BEFORE INSERT OR UPDATE OR DELETE ON operation_file_links
        FOR EACH ROW EXECUTE FUNCTION coinpup_guard_operation_file_link()
    """)
    op.execute("""
        CREATE FUNCTION coinpup_check_upload_completion() RETURNS trigger
        LANGUAGE plpgsql SET search_path = pg_catalog AS $$
        BEGIN
            IF NEW.state = 'ready' THEN
                IF NOT EXISTS (SELECT 1 FROM public.stored_files WHERE id = NEW.file_id
                    AND ledger_id = NEW.ledger_id AND byte_size = NEW.declared_size) THEN
                    RAISE EXCEPTION USING ERRCODE = '23514',
                        MESSAGE = 'A completed upload matches its stored file and declared size',
                        CONSTRAINT = 'ck_file_uploads_stored_content';
                END IF;
                IF NEW.operation_id IS NOT NULL AND NOT EXISTS (
                    SELECT 1 FROM public.operation_file_links WHERE file_id = NEW.file_id
                    AND operation_id = NEW.operation_id AND ledger_id = NEW.ledger_id
                ) THEN
                    RAISE EXCEPTION USING ERRCODE = '23514',
                        MESSAGE = 'A completed operation upload retains its file link',
                        CONSTRAINT = 'ck_file_uploads_operation_link';
                END IF;
            END IF;
            RETURN NULL;
        END;
        $$
    """)
    op.execute("""
        CREATE CONSTRAINT TRIGGER trg_file_uploads_completion
        AFTER INSERT OR UPDATE ON file_uploads DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION coinpup_check_upload_completion()
    """)


def downgrade() -> None:
    op.execute("""
        LOCK TABLE file_uploads, operation_file_links, stored_files IN ACCESS EXCLUSIVE MODE;
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM file_uploads) OR EXISTS (SELECT 1 FROM operation_file_links)
                OR EXISTS (SELECT 1 FROM stored_files) THEN
                RAISE EXCEPTION USING ERRCODE = '23514',
                    MESSAGE = 'Document history prevents this downgrade',
                    CONSTRAINT = 'ck_documents_downgrade';
            END IF;
        END $$
    """)
    op.drop_table("file_uploads")
    op.drop_table("operation_file_links")
    op.drop_table("stored_files")
    op.execute("DROP FUNCTION coinpup_check_upload_completion()")
    op.execute("DROP FUNCTION coinpup_guard_file_upload()")
    op.execute("DROP FUNCTION coinpup_guard_operation_file_link()")
    op.execute("DROP FUNCTION coinpup_guard_stored_file()")
