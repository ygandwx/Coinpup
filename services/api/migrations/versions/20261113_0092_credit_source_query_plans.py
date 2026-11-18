"""Reuse the identical source query plan across repeated native credit proofs."""

from pathlib import Path

from alembic import op

revision = "20261113_0092"
down_revision = "20261113_0091"
branch_labels = None
depends_on = None

_FROZEN = Path(__file__).resolve().parent / "sql/0092"
_OLD = (_FROZEN / "sources.sql").read_text(encoding="utf-8")
_OLD_POSTINGS = (_FROZEN / "postings.sql").read_text(encoding="utf-8")


def _prepared_query(definition):
    # Reuse PostgreSQL's statement plan. Every call reads the current transaction's rows.
    return (
        definition.replace(" LANGUAGE sql\n", " LANGUAGE plpgsql\n", 1)
        .replace("AS $function$\n", "AS $function$\nBEGIN RETURN QUERY\n", 1)
        .replace("$function$;", "END; $function$;", 1)
    )


def upgrade():
    op.execute("SELECT pg_advisory_xact_lock(18945999704708432)")
    op.execute(_prepared_query(_OLD_POSTINGS))
    op.execute(_prepared_query(_OLD))


def downgrade():
    op.execute("SELECT pg_advisory_xact_lock(18945999704708432)")
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM public.business_document_revisions) THEN
                RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='Reviewed source history exists',
                    CONSTRAINT='ck_credit_source_plan_downgrade';
            END IF;
        END; $$;
    """)
    op.execute(_OLD)
    op.execute(_OLD_POSTINGS)
