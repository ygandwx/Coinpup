"""Durable outcomes for device pushes and immutable acknowledged conflict decisions."""

from pathlib import Path

from alembic import op

revision = "20261125_0110"
down_revision = "20261125_0109"
branch_labels = None
depends_on = None

_SQL = Path(__file__).with_name("sql") / "0110" / "financial_device_push.sql"


def upgrade():
    op.execute("SELECT pg_advisory_xact_lock(18945999704708432)")
    op.execute(_SQL.read_text(encoding="utf-8"))


def downgrade():
    op.execute("SELECT pg_advisory_xact_lock(18945999704708432)")
    op.execute("""
        LOCK TABLE public.sync_financial_pushes, public.sync_financial_conflict_resolutions
            IN ACCESS EXCLUSIVE MODE;
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM public.sync_financial_pushes) THEN
                RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='Device push history exists',
                    CONSTRAINT='ck_sync_financial_push_downgrade';
            END IF;
        END; $$;
    """)
    op.drop_table("sync_financial_conflict_resolutions")
    op.drop_table("sync_financial_pushes")
    op.execute("DROP FUNCTION public.coinpup_guard_sync_financial_push()")
    op.execute("DROP FUNCTION public.coinpup_guard_sync_financial_resolution()")
