"""Build and verify permanent return revision receipts from the exact preceding group."""

from alembic import op

revision = "20261109_0068"
down_revision = "20261109_0067"
branch_labels = None
depends_on = None


_RESPONSE = r"""
CREATE FUNCTION coinpup_business_return_revision_response(revision_identifier uuid) RETURNS jsonb
LANGUAGE plpgsql STABLE SECURITY INVOKER SET search_path=pg_catalog AS $$
DECLARE h public.business_return_revisions%ROWTYPE;
    origin public.business_allocation_returns%ROWTYPE;
    previous public.business_return_revisions%ROWTYPE; prior_date date; prior_reason text;
    payment_asset text; invalid boolean; members_json jsonb;
BEGIN
    SELECT * INTO h FROM public.business_return_revisions WHERE id=revision_identifier;
    SELECT * INTO origin FROM public.business_allocation_returns
        WHERE (id,payment_id,ledger_id)=(h.return_id,h.payment_id,h.ledger_id);
    SELECT asset_id INTO payment_asset FROM public.business_payments
        WHERE (id,ledger_id)=(h.payment_id,h.ledger_id);
    IF h.group_version=2 THEN
        prior_date := origin.return_date; prior_reason := origin.reason;
    ELSE
        SELECT * INTO previous FROM public.business_return_revisions
        WHERE (id,return_id,payment_id,ledger_id)=(h.previous_revision_id,h.return_id,
            h.payment_id,h.ledger_id) AND group_version::bigint=h.group_version::bigint-1
            AND action='correct';
        prior_date := previous.return_date; prior_reason := previous.return_reason;
    END IF;
    IF h.id IS NULL OR origin.id IS NULL OR payment_asset IS NULL
       OR prior_date IS NULL OR prior_reason IS NULL THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='Return revision receipt is inconsistent',
            CONSTRAINT='ck_return_revision_shape';
    END IF;
    WITH members AS MATERIALIZED (
        SELECT m.*,row_number() OVER (ORDER BY m.member_no) AS pos,
               lag(m.previous_line_no) OVER (PARTITION BY m.action ORDER BY m.member_no) AS last_pos
        FROM public.business_return_revision_members m WHERE m.revision_id=h.id
    ), prior_ids AS MATERIALIZED (
        SELECT l.journal_id FROM public.business_allocation_return_lines l
        WHERE h.group_version=2 AND (l.return_id,l.payment_id,l.ledger_id)
            =(h.return_id,h.payment_id,h.ledger_id)
        UNION ALL
        SELECT m.journal_id FROM public.business_return_revision_members m
        WHERE m.revision_id=h.previous_revision_id AND m.action IN ('create','correct')
          AND (m.return_id,m.payment_id,m.ledger_id)=(h.return_id,h.payment_id,h.ledger_id)
    ), prior AS MATERIALIZED (
        SELECT f.* FROM public.coinpup_return_posting_facts(h.ledger_id,NULL,
            ARRAY(SELECT journal_id FROM prior_ids)) f
    ), prior_matches AS (
        SELECT m.id,count(f.journal_id) AS n FROM members m
        LEFT JOIN prior f ON f.journal_id=m.previous_journal_id GROUP BY m.id
    ), counts AS (
        SELECT count(*) AS n,count(*) FILTER (WHERE action<>'create') AS n_old,
               count(*) FILTER (WHERE action<>'cancel') AS n_new FROM members
    )
    SELECT (SELECT n NOT BETWEEN 1 AND 400 OR n_old NOT BETWEEN 1 AND 200
               OR n_old<>(SELECT count(*) FROM prior_ids) OR n_new>200
               OR (n_new=0)<>(h.action='cancel') FROM counts)
           OR EXISTS (SELECT 1 FROM members m JOIN members other
               ON other.source_operation_id=m.operation_id)
           OR EXISTS (SELECT j.id FROM members m CROSS JOIN LATERAL
               unnest(ARRAY[m.previous_journal_id,m.reversal_journal_id,m.journal_id]) j(id)
               WHERE j.id IS NOT NULL GROUP BY j.id HAVING count(*)<>1)
           OR coalesce(bool_or(m.pos<>m.member_no OR a.id IS NULL OR d.id IS NULL
               OR (m.return_id,m.payment_id,m.ledger_id)
                  IS DISTINCT FROM (h.return_id,h.payment_id,h.ledger_id)
               OR (m.action<>'cancel' AND m.line_no<>m.pos)
               OR (m.action='cancel' AND
                 (m.pos<=(SELECT n_new FROM counts) OR m.previous_line_no<=m.last_pos))
               OR (m.action<>'create' AND (x.n<>1 OR
                 (f.return_id,f.payment_id,f.source_allocation_line_id,f.source_operation_id,
                  f.source_version,f.source_return_no,f.operation_id,f.operation_version,
                  f.line_no,f.group_version::bigint)
                 IS DISTINCT FROM (h.return_id,h.payment_id,m.source_allocation_line_id,
                  m.source_operation_id,m.source_version,m.source_return_no,m.operation_id,
                  m.previous_operation_version,m.previous_line_no,h.group_version::bigint-1)))
           ),false),
        jsonb_agg(jsonb_build_object(
            'action',m.action,'source_allocation_line_id',m.source_allocation_line_id,
            'source_operation_id',m.source_operation_id,'source_version',m.source_version,
            'source_return_no',m.source_return_no,'document_id',a.document_id,
            'document_line_id',a.document_line_id,'document_asset_id',d.asset_id,
            'operation_id',m.operation_id,'operation_version',m.operation_version,
            'previous',CASE WHEN m.action<>'create' THEN jsonb_build_object(
                'line_no',f.line_no,'operation_version',f.operation_version,
                'journal_id',f.journal_id,
                'payment_amount',f.payment_amount::text,'document_amount',f.document_amount::text)
                END,
            'reversal_journal_id',m.reversal_journal_id,
            'replacement',CASE WHEN m.action<>'cancel' THEN jsonb_build_object(
                'line_no',m.line_no,'operation_version',m.operation_version,
                'journal_id',m.journal_id,
                'payment_amount',m.payment_amount::text,'document_amount',m.document_amount::text)
                END) ORDER BY m.member_no)
        INTO invalid,members_json FROM members m JOIN prior_matches x ON x.id=m.id
        LEFT JOIN prior f ON f.journal_id=m.previous_journal_id
        LEFT JOIN public.business_allocation_lines a
          ON (a.id,a.operation_id,a.payment_id,a.ledger_id)
            =(m.source_allocation_line_id,m.source_operation_id,h.payment_id,h.ledger_id)
        LEFT JOIN public.business_documents d ON (d.id,d.ledger_id)=(a.document_id,h.ledger_id);
    IF invalid THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='Return revision receipt is inconsistent',
            CONSTRAINT='ck_return_revision_shape';
    END IF;
    RETURN jsonb_build_object('kind','business_allocation_return_revision','return_id',h.return_id,
        'revision_id',h.id,'ledger_id',h.ledger_id,'payment_id',h.payment_id,
        'payment_asset_id',payment_asset,
        'previous_version',h.group_version-1,'version',h.group_version,
        'action',h.action,'reason',h.revision_reason,
        'previous',jsonb_build_object('return_date',prior_date,'reason',prior_reason),
        'replacement',CASE WHEN h.action='correct' THEN jsonb_build_object(
            'return_date',h.return_date,'reason',h.return_reason) END,'members',members_json);
END; $$;
"""

_RECEIPT = r"""
CREATE FUNCTION coinpup_validate_return_revision_receipt(revision_identifier uuid) RETURNS void
LANGUAGE plpgsql STABLE SECURITY INVOKER SET search_path=pg_catalog AS $$
DECLARE h public.business_return_revisions%ROWTYPE; receipt public.command_receipts%ROWTYPE;
    anchor uuid;
BEGIN
    SELECT * INTO h FROM public.business_return_revisions WHERE id=revision_identifier;
    SELECT * INTO receipt FROM public.command_receipts
        WHERE ledger_id=h.ledger_id AND key=h.idempotency_key;
    SELECT operation_id INTO anchor FROM public.business_return_revision_members
        WHERE revision_id=h.id AND member_no=1;
    IF h.id IS NULL OR anchor IS NULL OR receipt.hash_version IS DISTINCT FROM 2
       OR receipt.response_status IS DISTINCT FROM 200
       OR receipt.operation_id IS DISTINCT FROM anchor
       OR receipt.response::text IS DISTINCT FROM public.coinpup_business_return_revision_response(
            h.id)::text THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='Return revision receipt is inconsistent',
            CONSTRAINT='ck_return_revision_shape';
    END IF;
END; $$;
"""


def upgrade():
    op.execute("SELECT pg_advisory_xact_lock(18945999704708432)")
    op.execute(_RESPONSE)
    op.execute(_RECEIPT)


def downgrade():
    op.execute("SELECT pg_advisory_xact_lock(18945999704708432)")
    op.execute(
        "LOCK TABLE public.business_return_revisions, public.business_return_revision_members "
        "IN ACCESS EXCLUSIVE MODE"
    )
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM public.business_return_revisions)
                OR EXISTS (SELECT 1 FROM public.business_return_revision_members)
                OR EXISTS (SELECT 1 FROM public.change_log
                    WHERE entity_type IN
                        ('business_return_revisions','business_return_revision_members')
                        OR (entity_type='business_allocation_returns' AND entity_version>1)) THEN
                RAISE EXCEPTION USING ERRCODE='23514',
                    MESSAGE='Allocation return revision history prevents downgrade',
                    CONSTRAINT='ck_return_revision_downgrade';
            END IF;
        END; $$;
    """)
    op.execute("DROP FUNCTION coinpup_validate_return_revision_receipt(uuid)")
    op.execute("DROP FUNCTION coinpup_business_return_revision_response(uuid)")
