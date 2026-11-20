"""Reuse cached plans within the complete document revision proof."""

from alembic import op

revision = "20261120_0131"
down_revision = "20261113_0096"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("SELECT pg_advisory_xact_lock(18945999704708432)")
    # The proof repeatedly validates records by indexed identity. Reuse the
    # statement plans while reading fresh data from the caller's transaction.
    op.execute("""
        ALTER FUNCTION public.coinpup_validate_document_revision(uuid, uuid, uuid)
        SET plan_cache_mode = force_generic_plan
    """)


def downgrade():
    op.execute("SELECT pg_advisory_xact_lock(18945999704708432)")
    op.execute("LOCK TABLE public.business_document_revisions IN ACCESS EXCLUSIVE MODE")
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM public.business_document_revisions)
                OR EXISTS (SELECT 1 FROM public.change_log
                    WHERE entity_type='business_document_revisions') THEN
                RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='Document revision history exists',
                    CONSTRAINT='ck_document_group_plan_downgrade';
            END IF;
        END; $$;
    """)
    op.execute("""
        ALTER FUNCTION public.coinpup_validate_document_revision(uuid, uuid, uuid)
        RESET plan_cache_mode
    """)
