"""Persist verified parts and permanent receipts for resumable private uploads."""

from pathlib import Path

from alembic import op

revision = "20261125_0111"
down_revision = "20261125_0110"
branch_labels = None
depends_on = None

_SQL = Path(__file__).with_name("sql") / "0111"


def upgrade():
    op.execute("SELECT pg_advisory_xact_lock(18945999704708432)")
    op.execute((_SQL / "resumable_uploads.sql").read_text(encoding="utf-8"))


def downgrade():
    op.execute("SELECT pg_advisory_xact_lock(18945999704708432)")
    op.execute("""
        LOCK TABLE public.file_uploads, public.file_upload_resumes, public.file_upload_chunks
            IN ACCESS EXCLUSIVE MODE;
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM public.file_upload_resumes) THEN
                RAISE EXCEPTION USING ERRCODE = '23514', CONSTRAINT = 'ck_upload_resume_downgrade',
                    MESSAGE = 'Resumable upload history prevents downgrade';
            END IF;
        END; $$;
    """)
    op.execute("DROP TRIGGER tr_resumable_upload_complete ON public.file_uploads")
    op.drop_table("file_upload_chunks")
    op.drop_table("file_upload_resumes")
    for name in (
        "coinpup_guard_upload_resume",
        "coinpup_guard_upload_chunk",
        "coinpup_complete_resumable_upload",
        "coinpup_abandon_resumable_upload",
        "coinpup_resume_device_active",
    ):
        arguments = "uuid, uuid" if name == "coinpup_resume_device_active" else ""
        op.execute(f"DROP FUNCTION public.{name}({arguments})")
