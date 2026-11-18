"""Pin a new allocation to the exact replacement line reviewed at posting time."""

import sqlalchemy as sa
from alembic import op

revision = "20261110_0084"
down_revision = "20261110_0083"
branch_labels = None
depends_on = None


_BIND = """
CREATE FUNCTION coinpup_bind_allocation_document_fact() RETURNS trigger
LANGUAGE plpgsql SET search_path=pg_catalog AS $$
DECLARE head_id uuid; fact_id uuid;
BEGIN
    PERFORM pg_advisory_xact_lock(18945999704708432);
    IF current_setting('transaction_isolation') <> 'read committed' THEN
        RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='Invalid allocation source isolation',
            CONSTRAINT='ck_allocation_document_isolation';
    END IF;
    SELECT h.id INTO head_id FROM public.business_document_revisions h
    WHERE (h.document_id,h.ledger_id)=(NEW.document_id,NEW.ledger_id)
    ORDER BY h.document_version DESC LIMIT 1;
    IF FOUND THEN
        SELECT f.id INTO fact_id FROM public.business_document_revision_lines f
        JOIN public.business_document_accruals a ON (a.replacement_fact_id,a.revision_id,
            a.source_line_id,a.ledger_id)=(f.id,f.revision_id,f.source_line_id,f.ledger_id)
        WHERE (f.revision_id,f.document_id,f.source_line_id,f.ledger_id)=
            (head_id,NEW.document_id,NEW.document_line_id,NEW.ledger_id)
            AND f.total_amount>0 AND a.journal_id IS NOT NULL;
        IF NOT FOUND THEN
            RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='Allocation requires an effective line',
                CONSTRAINT='ck_allocation_document_fact';
        END IF;
    END IF;
    IF NEW.document_revision_line_id IS NOT NULL
        AND NEW.document_revision_line_id IS DISTINCT FROM fact_id THEN
        RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='Allocation has a different source version',
            CONSTRAINT='ck_allocation_document_fact';
    END IF;
    NEW.document_revision_line_id:=fact_id;
    RETURN NEW;
END; $$;
"""

_HISTORY = """
CREATE FUNCTION coinpup_allocation_document_facts(ledger_identifier uuid,allocation_identifier uuid,
    line_identifiers uuid[]) RETURNS TABLE (
    allocation_line_id uuid,allocation_id uuid,ledger_id uuid,document_id uuid,source_line_id uuid,
    document_version integer,document_kind varchar,party_id uuid,asset_id varchar,issue_date date,
    description varchar,project_id uuid,category_id uuid,recognition_date date,net_amount numeric,
    tax_amount numeric,total_amount numeric
) LANGUAGE sql STABLE SET search_path=pg_catalog AS $$
WITH selected AS MATERIALIZED (
    SELECT m.* FROM public.business_allocation_lines m WHERE m.ledger_id=ledger_identifier
        AND (allocation_identifier IS NOT NULL OR line_identifiers IS NOT NULL)
        AND (allocation_identifier IS NULL OR m.allocation_id=allocation_identifier)
        AND (line_identifiers IS NULL OR m.id=ANY(line_identifiers))
)
SELECT m.id,m.allocation_id,m.ledger_id,d.id,l.id,c.source_version+1,d.document_kind,d.party_id,
    d.asset_id,d.issue_date,l.description,l.project_id,l.category_id,l.recognition_date,
    l.net_amount,l.tax_amount,l.total_amount
FROM selected m JOIN public.business_documents d ON (d.id,d.ledger_id)=(m.document_id,m.ledger_id)
JOIN public.business_confirmations c ON (c.document_id,c.ledger_id)=(d.id,d.ledger_id)
JOIN public.business_document_lines l ON (l.id,l.document_id,l.ledger_id)=
    (m.document_line_id,d.id,d.ledger_id)
WHERE m.document_revision_line_id IS NULL AND l.total_amount>0 AND NOT l.archived
UNION ALL
SELECT m.id,m.allocation_id,m.ledger_id,h.document_id,f.source_line_id,h.document_version,
    d.document_kind,h.party_id,h.asset_id,h.issue_date,f.description,f.project_id,f.category_id,
    f.recognition_date,f.net_amount,f.tax_amount,f.total_amount
FROM selected m JOIN public.business_document_revision_lines f ON (f.id,f.source_line_id,
    f.document_id,f.ledger_id)=(m.document_revision_line_id,m.document_line_id,m.document_id,
    m.ledger_id)
JOIN public.business_document_revisions h ON (h.id,h.document_id,h.ledger_id)=
    (f.revision_id,f.document_id,f.ledger_id)
JOIN public.business_documents d ON (d.id,d.ledger_id)=(h.document_id,h.ledger_id)
WHERE h.action='correct' AND f.total_amount>0;
$$;
"""


def upgrade():
    op.execute("SELECT pg_advisory_xact_lock(18945999704708432)")
    op.add_column("business_allocation_lines", sa.Column("document_revision_line_id", sa.Uuid()))
    op.create_foreign_key(
        "fk_allocation_document_revision_line",
        "business_allocation_lines",
        "business_document_revision_lines",
        ["document_revision_line_id", "ledger_id"],
        ["id", "ledger_id"],
        ondelete="RESTRICT",
    )
    op.create_index(
        "ix_allocation_document_revision_line",
        "business_allocation_lines",
        ["document_revision_line_id", "ledger_id"],
        postgresql_where=sa.text("document_revision_line_id IS NOT NULL"),
    )
    op.execute(_BIND)
    op.execute(
        "CREATE TRIGGER tr_allocation_document_fact BEFORE INSERT ON business_allocation_lines "
        "FOR EACH ROW EXECUTE FUNCTION coinpup_bind_allocation_document_fact()"
    )
    op.execute(_HISTORY)


def downgrade():
    op.execute("SELECT pg_advisory_xact_lock(18945999704708432)")
    op.execute("LOCK TABLE public.business_allocation_lines IN ACCESS EXCLUSIVE MODE")
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM public.business_allocation_lines
                WHERE document_revision_line_id IS NOT NULL) THEN
                RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='Pinned allocation blocks downgrade',
                    CONSTRAINT='ck_allocation_document_downgrade';
            END IF;
        END; $$;
    """)
    op.execute("DROP FUNCTION coinpup_allocation_document_facts(uuid,uuid,uuid[])")
    op.execute("DROP TRIGGER tr_allocation_document_fact ON business_allocation_lines")
    op.execute("DROP FUNCTION coinpup_bind_allocation_document_fact()")
    op.drop_index("ix_allocation_document_revision_line", table_name="business_allocation_lines")
    op.drop_constraint(
        "fk_allocation_document_revision_line", "business_allocation_lines", type_="foreignkey"
    )
    op.drop_column("business_allocation_lines", "document_revision_line_id")
