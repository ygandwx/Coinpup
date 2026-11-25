"""Owner-scoped device registration and durable witnessed notification checkpoints."""

from pathlib import Path

from alembic import op

revision = "20261125_0109"
down_revision = "20261125_0108"
branch_labels = None
depends_on = None

_SQL = Path(__file__).with_name("sql") / "0109"


def upgrade():
    op.execute("SELECT pg_advisory_xact_lock(18945999704708432)")
    op.execute((_SQL / "device_checkpoints.sql").read_text(encoding="utf-8"))


def downgrade():
    op.execute("SELECT pg_advisory_xact_lock(18945999704708432)")
    op.execute("""
        LOCK TABLE public.sync_device_revisions, public.sync_page_acknowledgements
            IN ACCESS EXCLUSIVE MODE;
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM public.sync_device_revisions)
                OR EXISTS (SELECT 1 FROM public.sync_page_acknowledgements) THEN
                RAISE EXCEPTION USING ERRCODE = '23514', CONSTRAINT = 'ck_sync_devices_downgrade',
                    MESSAGE = 'Existing device history prevents downgrade';
            END IF;
        END; $$;
    """)
    op.drop_table("sync_page_acknowledgements")
    op.drop_table("sync_device_revisions")
    op.execute("DROP FUNCTION public.coinpup_check_sync_ack()")
    op.execute("DROP FUNCTION public.coinpup_seal_device_revision()")
