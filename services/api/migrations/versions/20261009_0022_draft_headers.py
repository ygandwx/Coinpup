"""Draft headers reuse scoped identities without making drafts postable."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "20261009_0022"
down_revision = "20261009_0021"
branch_labels = None
depends_on = None

_FIELDS = (
    ("document_kind", sa.String(7)),
    ("state", sa.String(5)),
    ("party_id", sa.Uuid()),
    ("asset_id", sa.String(200)),
    ("issue_date", sa.Date()),
    ("due_date", sa.Date()),
    ("notes", sa.String(2000)),
    ("issuer_snapshot", postgresql.JSONB()),
    ("party_snapshot", postgresql.JSONB()),
)
_PROFILE = (
    "(document_kind IS NULL AND state IS NULL AND party_id IS NULL AND asset_id IS NULL "
    "AND issue_date IS NULL AND due_date IS NULL AND notes IS NULL "
    "AND issuer_snapshot IS NULL AND party_snapshot IS NULL) OR "
    "(document_kind IS NOT NULL AND document_kind IN ('invoice', 'bill') "
    "AND state IS NOT NULL AND state = 'draft' AND party_id IS NOT NULL "
    "AND asset_id IS NOT NULL AND issue_date IS NOT NULL "
    "AND issuer_snapshot IS NOT NULL AND jsonb_typeof(issuer_snapshot) = 'object' "
    "AND party_snapshot IS NOT NULL AND jsonb_typeof(party_snapshot) = 'object')"
)
_GUARD = """
    CREATE FUNCTION coinpup_guard_document_draft() RETURNS trigger
    LANGUAGE plpgsql SET search_path = pg_catalog AS $$
    DECLARE mutable_fields text[] := ARRAY[
        'version', 'archived', 'updated_at', 'document_kind', 'state', 'party_id',
        'asset_id', 'issue_date', 'due_date', 'notes', 'issuer_snapshot', 'party_snapshot'
    ];
    BEGIN
        IF TG_OP = 'INSERT' THEN
            IF NEW.version = 1 AND NOT NEW.archived THEN RETURN NEW; END IF;
        ELSIF TG_OP = 'UPDATE' THEN
            IF NEW.version::bigint = OLD.version::bigint + 1
               AND (to_jsonb(NEW) - mutable_fields) IS NOT DISTINCT FROM
                   (to_jsonb(OLD) - mutable_fields)
               AND ((OLD.document_kind IS NULL AND NEW.document_kind IS NULL)
                    OR (OLD.state = 'draft' AND NEW.state = 'draft'
                        AND NEW.document_kind IS NOT NULL)) THEN
                RETURN NEW;
            END IF;
        END IF;
        RAISE EXCEPTION USING ERRCODE = '23514',
            MESSAGE = 'Business reference identity or version is immutable',
            CONSTRAINT = 'ck_business_reference_identity';
    END; $$
"""
_POSTING_GUARD = """
    CREATE FUNCTION coinpup_reject_draft_posting() RETURNS trigger
    LANGUAGE plpgsql SET search_path = pg_catalog AS $$
    BEGIN
        IF NEW.document_id IS NOT NULL AND EXISTS (
            SELECT 1 FROM public.business_documents d
            WHERE d.id = NEW.document_id AND d.ledger_id = NEW.ledger_id AND d.state = 'draft'
        ) THEN
            RAISE EXCEPTION USING ERRCODE = '23514',
                MESSAGE = 'A draft document cannot be posted',
                CONSTRAINT = 'ck_business_draft_not_postable';
        END IF;
        RETURN NEW;
    END; $$
"""


def upgrade():
    op.execute("SELECT pg_advisory_xact_lock(18945999704708432)")
    for name, type_ in _FIELDS:
        op.add_column("business_documents", sa.Column(name, type_, nullable=True))
    op.create_check_constraint("ck_business_documents_profile", "business_documents", _PROFILE)
    op.create_foreign_key(
        "fk_business_documents_party_ledger",
        "business_documents",
        "business_parties",
        ["party_id", "ledger_id"],
        ["id", "ledger_id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_business_documents_asset",
        "business_documents",
        "assets",
        ["asset_id"],
        ["asset_id"],
        ondelete="RESTRICT",
    )
    op.execute(_GUARD)
    op.execute("DROP TRIGGER tr_business_documents_identity ON business_documents")
    op.execute(
        "CREATE TRIGGER tr_business_documents_identity BEFORE INSERT OR UPDATE OR DELETE "
        "ON business_documents FOR EACH ROW EXECUTE FUNCTION coinpup_guard_document_draft()"
    )
    op.execute(_POSTING_GUARD)
    op.execute(
        "CREATE TRIGGER tr_journal_lines_no_draft BEFORE INSERT ON journal_lines "
        "FOR EACH ROW EXECUTE FUNCTION coinpup_reject_draft_posting()"
    )


def downgrade():
    op.execute("SELECT pg_advisory_xact_lock(18945999704708432)")
    op.execute("LOCK TABLE journal_lines, business_documents IN ACCESS EXCLUSIVE MODE")
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM public.business_documents WHERE document_kind IS NOT NULL)
            THEN
                RAISE EXCEPTION USING ERRCODE = '23514',
                    MESSAGE = 'Draft document history prevents downgrade',
                    CONSTRAINT = 'ck_document_draft_downgrade';
            END IF;
        END; $$
    """)
    op.execute("DROP TRIGGER tr_journal_lines_no_draft ON journal_lines")
    op.execute("DROP FUNCTION coinpup_reject_draft_posting()")
    op.execute("DROP TRIGGER tr_business_documents_identity ON business_documents")
    op.execute(
        "CREATE TRIGGER tr_business_documents_identity BEFORE INSERT OR UPDATE OR DELETE "
        "ON business_documents FOR EACH ROW EXECUTE FUNCTION coinpup_guard_business_reference()"
    )
    op.execute("DROP FUNCTION coinpup_guard_document_draft()")
    op.drop_constraint("fk_business_documents_asset", "business_documents", type_="foreignkey")
    op.drop_constraint(
        "fk_business_documents_party_ledger", "business_documents", type_="foreignkey"
    )
    op.drop_constraint("ck_business_documents_profile", "business_documents", type_="check")
    for name, _ in reversed(_FIELDS):
        op.drop_column("business_documents", name)
