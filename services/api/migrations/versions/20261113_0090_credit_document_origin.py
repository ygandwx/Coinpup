"""Keep original credit proofs and 201 receipts bound to their reviewed document header."""

from pathlib import Path

from alembic import op

revision = "20261113_0090"
down_revision = "20261113_0089"
branch_labels = None
depends_on = None

_FROZEN = Path(__file__).resolve().parent / "sql/0090"
_OLD_ORIGIN = (_FROZEN / "origin.sql").read_text(encoding="utf-8")
_OLD_RECEIPT = (_FROZEN / "receipt.sql").read_text(encoding="utf-8")
_ORIGIN = (
    _OLD_ORIGIN.replace(
        "receipt public.command_receipts%ROWTYPE; member_count integer; anchor uuid; item record;",
        "receipt public.command_receipts%ROWTYPE; member_count integer; anchor uuid; item record;\n"
        "    source_version integer; source_date date;",
    )
    .replace(
        "    SELECT * INTO receipt FROM public.command_receipts",
        """    IF entry.document_revision_id IS NULL THEN
        source_version:=document.version; source_date:=document.issue_date;
    ELSE
        SELECT h.document_version,h.issue_date INTO source_version,source_date
        FROM public.business_document_revisions h WHERE
            (h.id,h.document_id,h.ledger_id,h.action)=
            (entry.document_revision_id,entry.document_id,entry.ledger_id,'correct'::varchar);
    END IF;
    SELECT * INTO receipt FROM public.command_receipts""",
    )
    .replace(
        "entry.source_version IS DISTINCT FROM document.version",
        "entry.source_version IS DISTINCT FROM source_version",
    )
    .replace("entry.credit_date < document.issue_date", "entry.credit_date < source_date")
)
_RECEIPT = _OLD_RECEIPT.replace(
    "'asset_id',d.asset_id", "'asset_id',coalesce(h.asset_id,d.asset_id)"
).replace(
    "ON d.id=c.document_id AND d.ledger_id=c.ledger_id WHERE c.id=credit_identifier;",
    """ON d.id=c.document_id AND d.ledger_id=c.ledger_id
    LEFT JOIN public.business_document_revisions h ON
        (h.id,h.document_id,h.ledger_id)=(c.document_revision_id,c.document_id,c.ledger_id)
    WHERE c.id=credit_identifier;""",
)


def upgrade():
    op.execute("SELECT pg_advisory_xact_lock(18945999704708432)")
    op.execute(_ORIGIN)
    op.execute(_RECEIPT)
    op.execute(
        "ALTER FUNCTION public.coinpup_business_credit_response(uuid) SET DateStyle TO 'ISO, YMD'"
    )


def downgrade():
    op.execute("SELECT pg_advisory_xact_lock(18945999704708432)")
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM public.business_document_revisions) THEN
                RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='Reviewed credit history exists',
                    CONSTRAINT='ck_credit_document_origin_downgrade';
            END IF;
        END; $$;
    """)
    op.execute(_OLD_ORIGIN)
    op.execute(_OLD_RECEIPT)
