"""Validate each credit journal against its own immutable reviewed document source."""

from pathlib import Path

from alembic import op

revision = "20261113_0089"
down_revision = "20261113_0088"
branch_labels = None
depends_on = None

_OLD = (Path(__file__).resolve().parent / "sql/0089/credit-fact.sql").read_text(encoding="utf-8")
_FACT = (
    _OLD.replace(
        "document public.business_documents%ROWTYPE; "
        "source public.business_document_lines%ROWTYPE;\n"
        "    posted public.business_postings%ROWTYPE; "
        "original public.financial_operations%ROWTYPE;",
        "document public.business_documents%ROWTYPE; source record;\n"
        "    original public.financial_operations%ROWTYPE; "
        "source_journal public.journals%ROWTYPE;\n"
        "    bound_revision uuid;",
    )
    .replace(
        "fact record; fact_count bigint;",
        "fact record;",
    )
    .replace(
        """SELECT count(*) INTO fact_count FROM public.coinpup_credit_posting_facts(
        header.ledger_id,NULL,ARRAY[journal_identifier]);
    IF header.id IS NULL OR fact_count<>1 THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='Business credit shape is invalid',
            CONSTRAINT='ck_business_credit_shape';
    END IF;
    SELECT * INTO fact FROM public.coinpup_credit_posting_facts(
        header.ledger_id,NULL,ARRAY[journal_identifier]);""",
        """-- Count and read the one journal fact in the same statement.
    SELECT f.*,count(*) OVER () AS matched_count INTO fact
    FROM public.coinpup_credit_posting_facts(header.ledger_id,NULL,ARRAY[journal_identifier]) f;
    IF header.id IS NULL OR fact.matched_count IS DISTINCT FROM 1 THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='Business credit shape is invalid',
            CONSTRAINT='ck_business_credit_shape';
    END IF;""",
    )
    .replace(
        """SELECT * INTO source FROM public.business_document_lines
        WHERE id=fact.source_line_id AND document_id=fact.document_id
            AND ledger_id=fact.ledger_id;
    SELECT * INTO posted FROM public.business_postings WHERE line_id=source.id
        AND document_id=document.id AND ledger_id=fact.ledger_id;""",
        """IF fact.revision_id IS NULL THEN
        bound_revision := entry.document_revision_id;
    ELSE
        SELECT r.document_revision_id INTO bound_revision FROM public.business_credit_revisions r
        WHERE (r.id,r.credit_id,r.document_id,r.ledger_id)=
            (fact.revision_id,fact.credit_id,fact.document_id,fact.ledger_id);
    END IF;
    IF bound_revision IS NULL THEN
        -- The posting fact already identifies the original source. Use the same scoped
        -- confirmation and posting joins without re-enumerating the credit's posting facts.
        SELECT l.id,l.net_amount,l.tax_amount,l.total_amount,l.category_id,l.project_id,
            document.asset_id AS asset_id,document.party_id AS party_id,
            document.issue_date AS issue_date,c.source_version+1 AS document_version,
            p.operation_id AS source_operation_id,1 AS source_operation_version,
            p.journal_id AS source_journal_id,count(*) OVER () AS matches INTO source
        FROM public.business_document_lines l JOIN public.business_confirmations c
            ON (c.document_id,c.ledger_id)=(l.document_id,l.ledger_id)
        JOIN public.business_postings p ON (p.line_id,p.document_id,p.ledger_id,p.confirmation_id)=
            (l.id,l.document_id,l.ledger_id,c.id)
        WHERE (l.id,l.document_id,l.ledger_id)=(fact.source_line_id,fact.document_id,fact.ledger_id)
            AND l.total_amount>0 AND NOT l.archived;
    ELSE
        SELECT s.source_line_id AS id,s.net_amount,s.tax_amount,s.total_amount,
            s.category_id,s.project_id,s.asset_id,s.party_id,s.issue_date,s.document_version,
            s.source_operation_id,s.source_operation_version,s.source_journal_id,
            count(*) OVER () AS matches INTO source
        FROM public.coinpup_credit_document_sources(
            fact.ledger_id,NULL,ARRAY[journal_identifier]) s;
    END IF;
    SELECT * INTO source_journal FROM public.journals WHERE id=source.source_journal_id;""",
    )
    .replace("WHERE asset_id=document.asset_id", "WHERE asset_id=source.asset_id")
    .replace(
        "source.id IS NULL OR posted.id IS NULL",
        "source.id IS NULL OR source.matches<>1 OR source_journal.id IS NULL",
    )
    .replace(
        "OR entry.source_version IS DISTINCT FROM document.version",
        "OR (fact.revision_id IS NULL AND entry.source_version "
        "IS DISTINCT FROM source.document_version)",
    )
    .replace("fact.credit_date < document.issue_date", "fact.credit_date < source.issue_date")
    .replace(
        "fact.source_operation_id IS DISTINCT FROM posted.operation_id",
        "fact.source_operation_id IS DISTINCT FROM source.source_operation_id",
    )
    .replace(
        """OR original.version IS DISTINCT FROM 1 OR original.status IS DISTINCT FROM 'active'
       OR original.current_journal_id IS DISTINCT FROM posted.journal_id""",
        """OR original.version<source.source_operation_version
       OR (original.version=source.source_operation_version AND
           (original.status,original.current_journal_id) IS DISTINCT FROM
               ('active'::varchar,source.source_journal_id))
       OR (source_journal.operation_id,source_journal.ledger_id,source_journal.operation_version,
           source_journal.journal_kind,source_journal.reverses_journal_id) IS DISTINCT FROM
          (fact.source_operation_id,fact.ledger_id,source.source_operation_version,
           'posting'::varchar,NULL::uuid)""",
    )
    .replace(
        "l.asset_id IS DISTINCT FROM document.asset_id",
        "l.asset_id IS DISTINCT FROM source.asset_id",
    )
    .replace(
        "l.party_id IS DISTINCT FROM document.party_id",
        "l.party_id IS DISTINCT FROM source.party_id",
    )
    .replace(
        "    -- Only the real common result proves balance;",
        """    -- Sealed source journals are immutable and already proved at their own commit.
    IF original.version>source.source_operation_version THEN
        PERFORM public.coinpup_validate_document_operation_head(original.id);
    END IF;
    -- A same-transaction source must prove its exact accrual facts before credit validation.
    IF NOT source_journal.sealed THEN
        PERFORM public.coinpup_validate_document_accrual_fact(source_journal.id);
    END IF;
    -- Only the real common result proves balance;""",
    )
)


def upgrade():
    op.execute("SELECT pg_advisory_xact_lock(18945999704708432)")
    op.execute(_FACT)


def downgrade():
    op.execute("SELECT pg_advisory_xact_lock(18945999704708432)")
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM public.business_document_revisions) THEN
                RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='Versioned credit sources exist',
                    CONSTRAINT='ck_credit_document_validation_downgrade';
            END IF;
        END; $$;
    """)
    op.execute(_OLD)
