"""Commit complete document revisions only after their full native group proof."""

from pathlib import Path

from alembic import op

revision = "20261113_0096"
down_revision = "20261113_0095"
branch_labels = None
depends_on = None

_OLD = (Path(__file__).resolve().parent / "sql/0096/gate.sql").read_text(encoding="utf-8")
_GATE = """
CREATE OR REPLACE FUNCTION coinpup_check_document_revision() RETURNS trigger
LANGUAGE plpgsql SET search_path=pg_catalog AS $$
BEGIN
    PERFORM public.coinpup_validate_document_revision(NEW.ledger_id,NEW.document_id,NEW.id);
    RETURN NULL;
END; $$;
"""


def upgrade():
    op.execute("SELECT pg_advisory_xact_lock(18945999704708432)")
    op.execute(_GATE)


def downgrade():
    op.execute("SELECT pg_advisory_xact_lock(18945999704708432)")
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM public.business_document_revisions) THEN
                RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='Committed document history exists',
                    CONSTRAINT='ck_document_lifecycle_downgrade';
            END IF;
        END; $$;
    """)
    op.execute(_OLD)
