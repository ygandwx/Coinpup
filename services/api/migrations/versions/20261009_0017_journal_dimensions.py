"""Add scoped nullable dimensions and an independent exact reversal check."""

import sqlalchemy as sa
from alembic import op

revision = "20261009_0017"
down_revision = "20261009_0016"
branch_labels = None
depends_on = None

_COLUMNS = (
    "party_id",
    "counterparty_entity_id",
    "document_id",
    "document_line_id",
    "dimension_owner_id",
)
_FOREIGN_KEYS = (
    ("party_ledger", "business_parties", ["party_id", "ledger_id"], ["id", "ledger_id"]),
    ("document_ledger", "business_documents", ["document_id", "ledger_id"], ["id", "ledger_id"]),
    (
        "document_line",
        "business_document_lines",
        ["document_line_id", "document_id", "ledger_id"],
        ["id", "document_id", "ledger_id"],
    ),
    ("dimension_owner", "ledgers", ["ledger_id", "dimension_owner_id"], ["id", "owner_id"]),
    (
        "counterparty_owner",
        "entities",
        ["counterparty_entity_id", "dimension_owner_id"],
        ["id", "owner_id"],
    ),
)
_CHECKS = {
    "document_dimension": "document_line_id IS NULL OR document_id IS NOT NULL",
    "owner_dimension": "(counterparty_entity_id IS NULL) = (dimension_owner_id IS NULL)",
}
_REVERSAL_SQL = """
    CREATE FUNCTION coinpup_validate_reversal_dimensions() RETURNS trigger
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


def upgrade():
    op.execute("SELECT pg_advisory_xact_lock(18945999704708432)")
    for column in _COLUMNS:
        op.add_column("journal_lines", sa.Column(column, sa.Uuid(), nullable=True))
    for name, target, columns, references in _FOREIGN_KEYS:
        op.create_foreign_key(
            f"fk_journal_lines_{name}",
            "journal_lines",
            target,
            columns,
            references,
            ondelete="RESTRICT",
        )
    for name, condition in _CHECKS.items():
        op.create_check_constraint(f"ck_journal_lines_{name}", "journal_lines", condition)
    op.execute(_REVERSAL_SQL)
    op.execute(
        "CREATE CONSTRAINT TRIGGER tr_journals_reversal_dimensions AFTER INSERT ON journals "
        "DEFERRABLE INITIALLY DEFERRED FOR EACH ROW "
        "EXECUTE FUNCTION coinpup_validate_reversal_dimensions()"
    )


def downgrade():
    op.execute("SELECT pg_advisory_xact_lock(18945999704708432)")
    op.execute("LOCK TABLE journals, journal_lines IN ACCESS EXCLUSIVE MODE")
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM public.journal_lines WHERE party_id IS NOT NULL
                OR counterparty_entity_id IS NOT NULL OR document_id IS NOT NULL
                OR document_line_id IS NOT NULL OR dimension_owner_id IS NOT NULL) THEN
                RAISE EXCEPTION USING ERRCODE = '23514',
                    MESSAGE = 'Journal dimensions prevent downgrade',
                    CONSTRAINT = 'ck_journal_dimensions_downgrade';
            END IF;
        END; $$
    """)
    op.execute("DROP TRIGGER tr_journals_reversal_dimensions ON journals")
    op.execute("DROP FUNCTION coinpup_validate_reversal_dimensions()")
    for name in _CHECKS:
        op.drop_constraint(f"ck_journal_lines_{name}", "journal_lines", type_="check")
    for name, *_ in _FOREIGN_KEYS:
        op.drop_constraint(f"fk_journal_lines_{name}", "journal_lines", type_="foreignkey")
    for column in reversed(_COLUMNS):
        op.drop_column("journal_lines", column)
