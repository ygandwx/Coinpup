"""Project only effective, ledger-owned document lines for settlement and capacity queries."""

from alembic import op

revision = "20261110_0083"
down_revision = "20261110_0082"
branch_labels = None
depends_on = None


_CURRENT = """
CREATE FUNCTION coinpup_effective_document_lines(ledger_identifier uuid,document_identifier uuid,
    line_identifiers uuid[]) RETURNS TABLE (
    ledger_id uuid,confirmation_id uuid,document_id uuid,document_version integer,
    document_kind varchar,party_id uuid,asset_id varchar,issue_date date,
    source_line_id uuid,line_no integer,description varchar,category_id uuid,category_kind varchar,
    project_id uuid,recognition_date date,net_amount numeric,tax_amount numeric,
    total_amount numeric,
    operation_id uuid,operation_version integer,journal_id uuid
) LANGUAGE sql STABLE SET search_path=pg_catalog AS $$
WITH documents AS MATERIALIZED (
    SELECT d.id,d.ledger_id,c.id AS confirmation_id,d.document_kind,
        d.version AS original_version,d.party_id AS original_party,d.asset_id AS original_asset,
        d.issue_date AS original_date,r.id AS revision_id,r.document_version,r.action,
        r.party_id,r.asset_id,r.issue_date
    FROM public.business_documents d JOIN public.business_confirmations c
        ON (c.document_id,c.ledger_id)=(d.id,d.ledger_id)
    LEFT JOIN LATERAL (
        SELECT h.id,h.document_version,h.action,h.party_id,h.asset_id,h.issue_date
        FROM public.business_document_revisions h
        WHERE (h.document_id,h.ledger_id,h.confirmation_id)=(d.id,d.ledger_id,c.id)
        ORDER BY h.document_version DESC LIMIT 1
    ) r ON true
    WHERE d.ledger_id=ledger_identifier AND d.state='confirmed' AND NOT d.archived
        AND (document_identifier IS NULL OR d.id=document_identifier)
        AND (document_identifier IS NOT NULL OR d.id IN (
            SELECT s.document_id FROM public.business_document_lines s
            WHERE s.ledger_id=ledger_identifier AND s.id=ANY(line_identifiers)))
)
SELECT d.ledger_id,d.confirmation_id,d.id,d.original_version,d.document_kind,d.original_party,
    d.original_asset,d.original_date,l.id,l.line_no,l.description,l.category_id,l.category_kind,
    l.project_id,l.recognition_date,l.net_amount,l.tax_amount,l.total_amount,p.operation_id,
    CASE WHEN p.id IS NOT NULL THEN 1 END,p.journal_id
FROM documents d JOIN public.business_document_lines l
    ON (l.document_id,l.ledger_id)=(d.id,d.ledger_id)
LEFT JOIN public.business_postings p ON (p.confirmation_id,p.ledger_id,p.line_id)=
    (d.confirmation_id,d.ledger_id,l.id)
WHERE d.revision_id IS NULL AND NOT l.archived AND l.quantity IS NOT NULL
    AND (l.total_amount>0)=(p.journal_id IS NOT NULL)
    AND (line_identifiers IS NULL OR l.id=ANY(line_identifiers))
UNION ALL
SELECT d.ledger_id,d.confirmation_id,d.id,d.document_version,d.document_kind,d.party_id,
    d.asset_id,d.issue_date,f.source_line_id,f.line_no,f.description,f.category_id,f.category_kind,
    f.project_id,f.recognition_date,f.net_amount,f.tax_amount,f.total_amount,a.operation_id,
    a.operation_version,a.journal_id
FROM documents d JOIN public.business_document_revision_lines f
    ON (f.revision_id,f.ledger_id,f.document_id)=(d.revision_id,d.ledger_id,d.id)
LEFT JOIN public.business_document_accruals a ON (a.replacement_fact_id,a.revision_id,
    a.ledger_id,a.source_line_id)=(f.id,f.revision_id,f.ledger_id,f.source_line_id)
WHERE d.action='correct' AND (f.total_amount>0)=(a.journal_id IS NOT NULL)
    AND (line_identifiers IS NULL OR f.source_line_id=ANY(line_identifiers));
$$;
"""


def upgrade():
    op.execute("SELECT pg_advisory_xact_lock(18945999704708432)")
    op.execute(_CURRENT)


def downgrade():
    op.execute("SELECT pg_advisory_xact_lock(18945999704708432)")
    op.execute("DROP FUNCTION coinpup_effective_document_lines(uuid,uuid,uuid[])")
