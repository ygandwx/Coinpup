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
        "    original public.financial_operations%ROWTYPE; source_journal public.journals%ROWTYPE;",
    )
    .replace(
        """SELECT * INTO source FROM public.business_document_lines
        WHERE id=fact.source_line_id AND document_id=fact.document_id
            AND ledger_id=fact.ledger_id;
    SELECT * INTO posted FROM public.business_postings WHERE line_id=source.id
        AND document_id=document.id AND ledger_id=fact.ledger_id;""",
        """SELECT s.*,s.source_line_id AS id,count(*) OVER () AS matches INTO source
    FROM public.coinpup_credit_document_sources(
        fact.ledger_id,NULL,ARRAY[journal_identifier]) s;
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
