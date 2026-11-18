"""Derive fixed confirmed-document receipts from immutable groups, operation links and amounts."""

from alembic import op

revision = "20261110_0079"
down_revision = "20261110_0078"
branch_labels = None
depends_on = None


_GROUP = """
CREATE FUNCTION coinpup_confirmed_document_group(ledger_identifier uuid, document_identifier uuid,
    requested_version integer) RETURNS jsonb LANGUAGE sql STABLE SET search_path=pg_catalog AS $$
WITH doc AS (
    SELECT d.*,c.id AS confirmation_id,c.source_version FROM public.business_documents d
    JOIN public.business_confirmations c ON (c.document_id,c.ledger_id)=(d.id,d.ledger_id)
    WHERE d.ledger_id=ledger_identifier AND d.id=document_identifier AND d.state='confirmed'
), h AS (
    SELECT NULL::uuid AS revision_id,d.confirmation_id,d.document_kind,d.party_id,d.asset_id,
        d.issue_date,d.due_date,d.notes
    FROM doc d WHERE d.source_version::bigint+1=requested_version
    UNION ALL
    SELECT r.id,r.confirmation_id,d.document_kind,r.party_id,r.asset_id,r.issue_date,r.due_date,
        r.notes FROM doc d JOIN public.business_document_revisions r
        ON (r.ledger_id,r.document_id,r.confirmation_id)=(d.ledger_id,d.id,d.confirmation_id)
    WHERE r.document_version=requested_version AND r.action='correct'
), sources AS (
    SELECT l.id,l.line_no,l.description,l.quantity,l.unit_price,l.discount_amount,
        l.tax_rate_percent,l.category_id,l.project_id,l.recognition_date,l.net_amount,l.tax_amount,l.total_amount,
        p.operation_id,1 AS operation_version,p.journal_id
    FROM h JOIN public.business_document_lines l ON h.revision_id IS NULL
        AND (l.document_id,l.ledger_id)=(document_identifier,ledger_identifier)
    LEFT JOIN public.business_postings p ON (p.confirmation_id,p.line_id,p.ledger_id)=
        (h.confirmation_id,l.id,ledger_identifier)
    WHERE NOT l.archived AND l.quantity IS NOT NULL
    UNION ALL
    SELECT f.source_line_id,f.line_no,f.description,f.quantity,f.unit_price,f.discount_amount,
        f.tax_rate_percent,f.category_id,f.project_id,f.recognition_date,
        f.net_amount,f.tax_amount,f.total_amount,a.operation_id,a.operation_version,a.journal_id
    FROM h JOIN public.business_document_revision_lines f
        ON (f.revision_id,f.ledger_id,f.document_id)=
           (h.revision_id,ledger_identifier,document_identifier)
    LEFT JOIN public.business_document_accruals a ON (a.replacement_fact_id,a.revision_id,
        a.source_line_id,a.ledger_id)=(f.id,f.revision_id,f.source_line_id,f.ledger_id)
)
SELECT jsonb_build_object('document_kind',h.document_kind,'party_id',h.party_id,
    'asset_id',h.asset_id,
    'issue_date',h.issue_date,'due_date',h.due_date,'notes',h.notes,'lines',g.lines,
    'net_amount',round(g.net,a.scale)::text,'tax_amount',round(g.tax,a.scale)::text,
    'total_amount',round(g.total,a.scale)::text)
FROM h JOIN public.assets a ON a.asset_id=h.asset_id CROSS JOIN LATERAL (
    SELECT count(*) AS members,min(s.line_no) AS first,max(s.line_no) AS last,
        bool_and((s.total_amount>0)=(s.journal_id IS NOT NULL)) AS postings_match,
        sum(s.net_amount) AS net,sum(s.tax_amount) AS tax,sum(s.total_amount) AS total,
        jsonb_agg(jsonb_build_object('id',s.id,'line_no',s.line_no,'description',s.description,
            'quantity',s.quantity,'unit_price',s.unit_price,'discount_amount',s.discount_amount,
            'tax_rate_percent',s.tax_rate_percent,'category_id',s.category_id,
            'project_id',s.project_id,'recognition_date',s.recognition_date,
            'net_amount',round(s.net_amount,a.scale)::text,
            'tax_amount',round(s.tax_amount,a.scale)::text,
            'total_amount',round(s.total_amount,a.scale)::text,'posting',
            CASE WHEN s.journal_id IS NOT NULL THEN jsonb_build_object(
                'operation_id',s.operation_id,
                'operation_version',s.operation_version,'journal_id',s.journal_id) END)
            ORDER BY s.line_no) AS lines
    FROM sources s
) g WHERE g.members BETWEEN 1 AND 200 AND g.total>0 AND g.total<100000000000000000000
    AND g.postings_match AND (h.revision_id IS NULL OR (g.first=1 AND g.last=g.members));
$$;
"""


_RECEIPT = """
CREATE FUNCTION coinpup_confirmed_document_revision_response(ledger_identifier uuid,
    document_identifier uuid, revision_identifier uuid) RETURNS jsonb
LANGUAGE plpgsql STABLE SET search_path=pg_catalog AS $$
DECLARE h public.business_document_revisions%ROWTYPE; before_group jsonb; after_group jsonb;
    reversals jsonb; valid boolean;
BEGIN
    SELECT * INTO h FROM public.business_document_revisions WHERE (id,ledger_id,document_id)=
        (revision_identifier,ledger_identifier,document_identifier);
    IF NOT FOUND THEN RETURN NULL; END IF;
    before_group := public.coinpup_confirmed_document_group(ledger_identifier,document_identifier,
        h.previous_version);
    IF h.action='correct' THEN
        after_group := public.coinpup_confirmed_document_group(
            ledger_identifier,document_identifier,h.document_version);
    END IF;
    IF before_group IS NULL OR (h.action='correct' AND after_group IS NULL) THEN
        RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='Incomplete document receipt sources',
            CONSTRAINT='ck_document_revision_response';
    END IF;
    WITH previous AS (
        SELECT (item->>'id')::uuid AS source,item->'posting' AS posting,ord
        FROM jsonb_array_elements(before_group->'lines') WITH ORDINALITY AS x(item,ord)
        WHERE jsonb_typeof(item->'posting')='object'
    ), replacement AS (
        SELECT (item->>'id')::uuid AS source,item->'posting' AS posting
        FROM jsonb_array_elements(after_group->'lines') AS x(item)
        WHERE jsonb_typeof(item->'posting')='object'
    ), expected AS (
        SELECT coalesce(p.source,r.source) AS source,p.posting AS old,r.posting AS new,p.ord
        FROM previous p FULL JOIN replacement r ON r.source=p.source
    ), actual AS (
        SELECT * FROM public.business_document_accruals a WHERE
            (a.revision_id,a.ledger_id,a.document_id)=(h.id,h.ledger_id,h.document_id)
    )
    SELECT bool_and(e.source IS NOT NULL AND a.id IS NOT NULL
        AND a.action=CASE WHEN e.old IS NULL THEN 'create'
            WHEN e.new IS NULL THEN 'cancel' ELSE 'correct' END
        AND (e.old IS NULL OR ((e.old->>'operation_id')::uuid=a.operation_id
            AND (e.old->>'operation_version')::bigint+1=a.operation_version
            AND (e.old->>'journal_id')::uuid=a.previous_journal_id))
        AND (e.new IS NULL OR ((e.new->>'operation_id')::uuid=a.operation_id
            AND (e.new->>'operation_version')::integer=a.operation_version
            AND (e.new->>'journal_id')::uuid=a.journal_id))),
        jsonb_agg(jsonb_build_object('source_line_id',e.source,'journal_id',a.reversal_journal_id)
            ORDER BY e.ord) FILTER (WHERE e.old IS NOT NULL)
    INTO valid,reversals FROM expected e FULL JOIN actual a ON a.source_line_id=e.source;
    IF valid IS NOT TRUE OR reversals IS NULL THEN
        RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='Incomplete document receipt membership',
            CONSTRAINT='ck_document_revision_response';
    END IF;
    RETURN jsonb_build_object('kind','business_document_revision','ledger_id',h.ledger_id,
        'confirmation_id',h.confirmation_id,'document_id',h.document_id,'revision_id',h.id,
        'previous_version',h.previous_version,'version',h.document_version,'action',h.action,
        'reason',h.reason,'previous',before_group,'replacement',after_group,'reversals',reversals);
END; $$;
"""


def upgrade():
    op.execute("SELECT pg_advisory_xact_lock(18945999704708432)")
    op.execute(_GROUP)
    op.execute(_RECEIPT)


def downgrade():
    op.execute("SELECT pg_advisory_xact_lock(18945999704708432)")
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM public.business_document_revisions)
                OR EXISTS (SELECT 1 FROM public.command_receipts
                    WHERE response->>'kind'='business_document_revision') THEN
                RAISE EXCEPTION USING ERRCODE='23514',
                    MESSAGE='Document receipts prevent downgrade',
                    CONSTRAINT='ck_document_revision_receipt_downgrade';
            END IF;
        END; $$;
    """)
    op.execute("DROP FUNCTION coinpup_confirmed_document_revision_response(uuid,uuid,uuid)")
    op.execute("DROP FUNCTION coinpup_confirmed_document_group(uuid,uuid,integer)")
