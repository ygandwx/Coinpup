"""Read each fixed credit revision in its own bound document asset."""

from pathlib import Path

from alembic import op

revision = "20261113_0093"
down_revision = "20261113_0092"
branch_labels = None
depends_on = None

_OLD = (Path(__file__).resolve().parent / "sql/0093/receipt.sql").read_text(encoding="utf-8")
_RECEIPT = (
    _OLD.replace(
        "invalid boolean; members_json jsonb;",
        "invalid boolean; members_json jsonb; source_asset varchar;",
    )
    .replace(
        "    IF entry.group_version=2 THEN",
        """    IF entry.document_revision_id IS NULL THEN
        source_asset:=document.asset_id;
    ELSE
        SELECT h.asset_id INTO source_asset FROM public.business_document_revisions h
        WHERE (h.id,h.document_id,h.ledger_id,h.action)=
            (entry.document_revision_id,entry.document_id,entry.ledger_id,'correct'::varchar);
    END IF;
    IF entry.group_version=2 THEN""",
    )
    .replace(
        "IF entry.id IS NULL OR origin.id IS NULL",
        "IF source_asset IS NULL OR entry.id IS NULL OR origin.id IS NULL",
    )
    .replace("'asset_id',document.asset_id", "'asset_id',source_asset")
)


def upgrade():
    op.execute("SELECT pg_advisory_xact_lock(18945999704708432)")
    op.execute(_RECEIPT)
    op.execute(
        "ALTER FUNCTION public.coinpup_business_credit_revision_response(uuid) "
        "SET DateStyle TO 'ISO, YMD'"
    )


def downgrade():
    op.execute("SELECT pg_advisory_xact_lock(18945999704708432)")
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM public.business_document_revisions) THEN
                RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='Bound credit receipts exist',
                    CONSTRAINT='ck_credit_bound_receipt_downgrade';
            END IF;
        END; $$;
    """)
    op.execute(_OLD)
