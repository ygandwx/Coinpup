"""Ledger-owned project dimensions preserve sealed and reversal facts."""

import sqlalchemy as sa
from alembic import op

revision = "20261009_0021"
down_revision = "20261009_0020"
branch_labels = None
depends_on = None

_OLD_REVERSAL_SQL = """
    CREATE OR REPLACE FUNCTION coinpup_validate_reversal_dimensions() RETURNS trigger
    LANGUAGE plpgsql SET search_path = pg_catalog AS $$
    BEGIN
        IF NEW.journal_kind = 'reversal' AND EXISTS (
            SELECT 1 FROM public.journal_lines original
            JOIN public.journal_lines reverse ON reverse.journal_id = NEW.id
                AND reverse.ledger_id = original.ledger_id AND reverse.line_no = original.line_no
            WHERE original.journal_id = NEW.reverses_journal_id AND (
                ROW(original.party_id, original.counterparty_entity_id, original.document_id,
                    original.document_line_id, original.dimension_owner_id)
                IS DISTINCT FROM
                ROW(reverse.party_id, reverse.counterparty_entity_id, reverse.document_id,
                    reverse.document_line_id, reverse.dimension_owner_id)
            )
        ) THEN
            RAISE EXCEPTION USING ERRCODE = '23514',
                MESSAGE = 'Reversal dimensions must match the original journal',
                CONSTRAINT = 'ck_journal_reversal_dimensions';
        END IF;
        RETURN NULL;
    END; $$
"""
_REVERSAL_SQL = """
    CREATE OR REPLACE FUNCTION coinpup_validate_reversal_dimensions() RETURNS trigger
    LANGUAGE plpgsql SET search_path = pg_catalog AS $$
    BEGIN
        IF NEW.journal_kind = 'reversal' AND EXISTS (
            SELECT 1 FROM public.journal_lines original
            JOIN public.journal_lines reverse ON reverse.journal_id = NEW.id
                AND reverse.ledger_id = original.ledger_id AND reverse.line_no = original.line_no
            WHERE original.journal_id = NEW.reverses_journal_id AND (
                ROW(original.party_id, original.counterparty_entity_id, original.document_id,
                    original.document_line_id, original.dimension_owner_id,
                    original.project_id)
                IS DISTINCT FROM
                ROW(reverse.party_id, reverse.counterparty_entity_id, reverse.document_id,
                    reverse.document_line_id, reverse.dimension_owner_id,
                    reverse.project_id)
            )
        ) THEN
            RAISE EXCEPTION USING ERRCODE = '23514',
                MESSAGE = 'Reversal dimensions must match the original journal',
                CONSTRAINT = 'ck_journal_reversal_dimensions';
        END IF;
        RETURN NULL;
    END; $$
"""


def upgrade():
    op.execute("SELECT pg_advisory_xact_lock(18945999704708432)")
    op.add_column("journal_lines", sa.Column("project_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        "fk_journal_lines_project_ledger",
        "journal_lines",
        "business_projects",
        ["project_id", "ledger_id"],
        ["id", "ledger_id"],
        ondelete="RESTRICT",
    )
    op.execute(_REVERSAL_SQL)


def downgrade():
    op.execute("SELECT pg_advisory_xact_lock(18945999704708432)")
    op.execute("LOCK TABLE journals, journal_lines IN ACCESS EXCLUSIVE MODE")
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM public.journal_lines WHERE project_id IS NOT NULL) THEN
                RAISE EXCEPTION USING ERRCODE = '23514',
                    MESSAGE = 'Project dimensions prevent downgrade',
                    CONSTRAINT = 'ck_project_dimensions_downgrade';
            END IF;
        END; $$
    """)
    op.execute(_OLD_REVERSAL_SQL)
    op.drop_constraint("fk_journal_lines_project_ledger", "journal_lines", type_="foreignkey")
    op.drop_column("journal_lines", "project_id")
