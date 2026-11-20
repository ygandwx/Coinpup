"""Bound accrual source lookups and reuse the group history's head proof."""

from pathlib import Path

from alembic import op

revision = "20261120_0132"
down_revision = "20261120_0131"
branch_labels = None
depends_on = None

_SQL = Path(__file__).with_name("sql") / "0132"


def upgrade():
    op.execute("SELECT pg_advisory_xact_lock(18945999704708432)")
    op.execute((_SQL / "accrual_source.sql").read_text(encoding="utf-8"))
    op.execute((_SQL / "group_proof.sql").read_text(encoding="utf-8"))


def downgrade():
    op.execute("SELECT pg_advisory_xact_lock(18945999704708432)")
    op.execute("LOCK TABLE public.business_document_accruals IN ACCESS EXCLUSIVE MODE")
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM public.business_document_accruals)
                OR EXISTS (SELECT 1 FROM public.change_log
                    WHERE entity_type='business_document_accruals') THEN
                RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='Accrual history prevents downgrade',
                    CONSTRAINT='ck_document_accrual_source_keys_downgrade';
            END IF;
        END; $$;
    """)
    op.execute((_SQL / "prior_group_proof.sql").read_text(encoding="utf-8"))
    op.execute((_SQL / "prior_accrual_source.sql").read_text(encoding="utf-8"))
