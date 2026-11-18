"""Bind each credit posting group to the reviewed confirmed document version."""

import sqlalchemy as sa
from alembic import op

revision = "20261113_0088"
down_revision = "20261110_0087"
branch_labels = None
depends_on = None

_TABLES = (("business_credits", "credit"), ("business_credit_revisions", "credit_revision"))

_BIND = """
CREATE FUNCTION public.coinpup_bind_credit_document_version() RETURNS trigger
LANGUAGE plpgsql SET search_path=pg_catalog AS $$
DECLARE source_id uuid; head public.business_document_revisions%ROWTYPE;
BEGIN
    PERFORM pg_advisory_xact_lock(18945999704708432);
    IF to_jsonb(NEW)->>'action'='cancel' THEN
        IF NEW.previous_revision_id IS NULL THEN
            SELECT c.document_revision_id INTO source_id FROM public.business_credits c
            WHERE (c.id,c.document_id,c.ledger_id)=(NEW.credit_id,NEW.document_id,NEW.ledger_id);
        ELSE
            SELECT c.document_revision_id INTO source_id FROM public.business_credit_revisions c
            WHERE (c.id,c.credit_id,c.document_id,c.ledger_id)=
                (NEW.previous_revision_id,NEW.credit_id,NEW.document_id,NEW.ledger_id);
        END IF;
    ELSE
        SELECT h.* INTO head FROM public.business_document_revisions h
        WHERE (h.document_id,h.ledger_id)=(NEW.document_id,NEW.ledger_id)
        ORDER BY h.document_version DESC LIMIT 1;
        IF FOUND THEN
            IF current_setting('transaction_isolation')<>'read committed'
                OR head.action<>'correct'
                OR (TG_TABLE_NAME='business_credits'
                    AND (to_jsonb(NEW)->>'source_version')::integer<>head.document_version) THEN
                RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='Credit source version is inactive',
                    CONSTRAINT='ck_credit_document_version';
            END IF;
            source_id:=head.id;
        END IF;
    END IF;
    IF NEW.document_revision_id IS NOT NULL
        AND NEW.document_revision_id IS DISTINCT FROM source_id THEN
        RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='Credit source version differs',
            CONSTRAINT='ck_credit_document_version';
    END IF;
    NEW.document_revision_id:=source_id;
    RETURN NEW;
END; $$;
"""

_FACTS = """
CREATE FUNCTION public.coinpup_credit_document_sources(ledger_identifier uuid,
    credit_identifiers uuid[],journal_identifiers uuid[]) RETURNS TABLE (
    credit_id uuid,revision_id uuid,journal_id uuid,ledger_id uuid,document_id uuid,
    document_version integer,source_line_id uuid,source_operation_id uuid,
    source_operation_version integer,source_journal_id uuid,document_kind varchar,party_id uuid,
    asset_id varchar,issue_date date,description varchar,category_id uuid,category_kind varchar,
    project_id uuid,recognition_date date,net_amount numeric,tax_amount numeric,total_amount numeric
) LANGUAGE sql STABLE SET search_path=pg_catalog AS $$
WITH postings AS MATERIALIZED (
    SELECT f.*,CASE WHEN f.revision_id IS NULL THEN c.document_revision_id
        ELSE r.document_revision_id END AS source_revision
    FROM public.coinpup_credit_posting_facts(
        ledger_identifier,credit_identifiers,journal_identifiers) f
    JOIN public.business_credits c ON (c.id,c.document_id,c.ledger_id)=
        (f.credit_id,f.document_id,f.ledger_id)
    LEFT JOIN public.business_credit_revisions r ON (r.id,r.credit_id,r.document_id,r.ledger_id)=
        (f.revision_id,f.credit_id,f.document_id,f.ledger_id)
)
SELECT p.credit_id,p.revision_id,p.journal_id,p.ledger_id,d.id,c.source_version+1,
    l.id,a.operation_id,1,a.journal_id,d.document_kind,d.party_id,d.asset_id,d.issue_date,
    l.description,l.category_id,l.category_kind,l.project_id,l.recognition_date,
    l.net_amount,l.tax_amount,l.total_amount
FROM postings p
JOIN public.business_documents d ON (d.id,d.ledger_id)=(p.document_id,p.ledger_id)
JOIN public.business_confirmations c ON (c.document_id,c.ledger_id)=(d.id,d.ledger_id)
JOIN public.business_document_lines l ON (l.id,l.document_id,l.ledger_id)=
    (p.source_line_id,p.document_id,p.ledger_id)
JOIN public.business_postings a ON (a.line_id,a.document_id,a.ledger_id,a.confirmation_id)=
    (l.id,l.document_id,l.ledger_id,c.id)
WHERE p.source_revision IS NULL AND l.total_amount>0 AND NOT l.archived
UNION ALL
SELECT p.credit_id,p.revision_id,p.journal_id,p.ledger_id,h.document_id,h.document_version,
    l.source_line_id,a.operation_id,a.operation_version,a.journal_id,d.document_kind,
    h.party_id,h.asset_id,h.issue_date,l.description,l.category_id,l.category_kind,l.project_id,
    l.recognition_date,l.net_amount,l.tax_amount,l.total_amount
FROM postings p
JOIN public.business_document_revisions h ON (h.id,h.document_id,h.ledger_id)=
    (p.source_revision,p.document_id,p.ledger_id)
JOIN public.business_documents d ON (d.id,d.ledger_id)=(h.document_id,h.ledger_id)
JOIN public.business_document_revision_lines l ON (l.revision_id,l.source_line_id,
    l.document_id,l.ledger_id)=(h.id,p.source_line_id,h.document_id,h.ledger_id)
JOIN public.business_document_accruals a ON (a.replacement_fact_id,a.revision_id,a.source_line_id,
    a.document_id,a.ledger_id)=(l.id,l.revision_id,l.source_line_id,l.document_id,l.ledger_id)
WHERE h.action='correct' AND l.total_amount>0 AND a.journal_id IS NOT NULL;
$$;
"""


def upgrade():
    op.execute("SELECT pg_advisory_xact_lock(18945999704708432)")
    op.execute(_BIND)
    for table, label in _TABLES:
        op.add_column(
            table, sa.Column("document_revision_id", sa.Uuid(), nullable=True), schema="public"
        )
        op.create_foreign_key(
            f"fk_{label}_document_revision",
            table,
            "business_document_revisions",
            ["document_revision_id", "ledger_id"],
            ["id", "ledger_id"],
            ondelete="RESTRICT",
            source_schema="public",
            referent_schema="public",
        )
        op.create_index(
            f"ix_{label}_document_revision",
            table,
            ["document_revision_id", "ledger_id"],
            postgresql_where=sa.text("document_revision_id IS NOT NULL"),
            schema="public",
        )
        op.execute(
            f"CREATE TRIGGER tr_{label}_document_version BEFORE INSERT ON public.{table} "
            "FOR EACH ROW EXECUTE FUNCTION public.coinpup_bind_credit_document_version()"
        )
    op.execute(_FACTS)


def downgrade():
    op.execute("SELECT pg_advisory_xact_lock(18945999704708432)")
    op.execute(
        "LOCK TABLE public.business_credits,public.business_credit_revisions "
        "IN ACCESS EXCLUSIVE MODE"
    )
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM public.business_credits WHERE document_revision_id IS NOT NULL)
                OR EXISTS (SELECT 1 FROM public.business_credit_revisions
                    WHERE document_revision_id IS NOT NULL) THEN
                RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='Bound credit sources exist',
                    CONSTRAINT='ck_credit_document_source_downgrade';
            END IF;
            IF EXISTS (SELECT 1 FROM public.business_credit_revisions)
                OR EXISTS (SELECT 1 FROM public.change_log
                    WHERE entity_type IN
                        ('business_credit_revisions','business_credit_revision_members')
                        OR (entity_type='business_credits' AND entity_version>1)) THEN
                RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='Credit revision history exists',
                    CONSTRAINT='ck_credit_revision_downgrade';
            END IF;
            IF EXISTS (SELECT 1 FROM public.business_document_revisions)
                OR EXISTS (SELECT 1 FROM public.change_log
                    WHERE entity_type='business_document_revisions') THEN
                RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='Document revision history exists',
                    CONSTRAINT='ck_document_revision_downgrade';
            END IF;
        END; $$;
    """)
    op.execute("DROP FUNCTION public.coinpup_credit_document_sources(uuid,uuid[],uuid[])")
    for table, label in reversed(_TABLES):
        op.execute(f"DROP TRIGGER tr_{label}_document_version ON public.{table}")
        op.drop_index(f"ix_{label}_document_revision", table_name=table, schema="public")
        op.drop_constraint(
            f"fk_{label}_document_revision", table, type_="foreignkey", schema="public"
        )
        op.drop_column(table, "document_revision_id", schema="public")
    op.execute("DROP FUNCTION public.coinpup_bind_credit_document_version()")
