"""Project immutable revision responses without opening the complete-group write gate."""

from alembic import op

revision = "20261106_0045"
down_revision = "20261106_0044"
branch_labels = None
depends_on = None

_RESPONSE = r"""
CREATE FUNCTION coinpup_business_credit_revision_response(revision_identifier uuid) RETURNS jsonb
LANGUAGE plpgsql STABLE SECURITY INVOKER SET search_path=pg_catalog AS $$
DECLARE entry public.business_credit_revisions%ROWTYPE; origin public.business_credits%ROWTYPE;
    document public.business_documents%ROWTYPE; previous public.business_credit_revisions%ROWTYPE;
    prior_date date; prior_reason text; invalid boolean; members_json jsonb;
BEGIN
    SELECT * INTO entry FROM public.business_credit_revisions WHERE id=revision_identifier;
    SELECT * INTO origin FROM public.business_credits
        WHERE id=entry.credit_id AND ledger_id=entry.ledger_id AND document_id=entry.document_id;
    SELECT * INTO document FROM public.business_documents
        WHERE id=entry.document_id AND ledger_id=entry.ledger_id;
    IF entry.group_version=2 THEN
        prior_date := origin.credit_date; prior_reason := origin.reason;
    ELSE
        SELECT * INTO previous FROM public.business_credit_revisions
            WHERE id=entry.previous_revision_id AND credit_id=entry.credit_id
                AND ledger_id=entry.ledger_id AND document_id=entry.document_id;
        prior_date := previous.credit_date; prior_reason := previous.credit_reason;
    END IF;
    IF entry.id IS NULL OR origin.id IS NULL OR document.id IS NULL OR prior_date IS NULL
       OR prior_reason IS NULL OR (entry.group_version=2 AND entry.previous_revision_id IS NOT NULL)
       OR (entry.group_version>2 AND (previous.id IS NULL OR previous.action<>'correct'
           OR previous.group_version::bigint<>entry.group_version::bigint-1)) THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='Credit revision response is inconsistent',
            CONSTRAINT='ck_credit_revision_shape';
    END IF;
    WITH members AS MATERIALIZED (
        SELECT * FROM public.business_credit_revision_members WHERE revision_id=entry.id
    ), facts AS MATERIALIZED (
        SELECT * FROM public.coinpup_credit_posting_facts(entry.ledger_id,NULL,
            coalesce((SELECT array_agg(previous_journal_id) FILTER (WHERE action<>'create')
                FROM members),ARRAY[]::uuid[]))
    ), matches AS (
        SELECT m.id,count(f.journal_id) AS n FROM members m
        LEFT JOIN facts f ON f.journal_id=m.previous_journal_id GROUP BY m.id
    )
    SELECT count(*) NOT BETWEEN 1 AND 400 OR coalesce(bool_or(
        ROW(m.ledger_id,m.credit_id,m.document_id)
            IS DISTINCT FROM ROW(entry.ledger_id,entry.credit_id,entry.document_id)
        OR (m.action<>'create' AND (x.n<>1 OR
            ROW(f.credit_id,f.document_id,f.source_line_id,f.source_operation_id,f.operation_id,
                f.line_no,f.operation_version,f.group_version::bigint)
            IS DISTINCT FROM ROW(entry.credit_id,entry.document_id,m.source_line_id,
                m.source_operation_id,m.operation_id,m.previous_line_no,
                m.previous_operation_version,entry.group_version::bigint-1)))),false),
        jsonb_agg(jsonb_build_object(
            'action',m.action,'source_line_id',m.source_line_id,
            'source_operation_id',m.source_operation_id,'operation_id',m.operation_id,
            'operation_version',m.operation_version,'previous_line_no',m.previous_line_no,
            'previous_operation_version',m.previous_operation_version,
            'previous_journal_id',m.previous_journal_id,'reversal_journal_id',m.reversal_journal_id,
            'previous_net_amount',f.net_amount::text,'previous_tax_amount',f.tax_amount::text,
            'previous_total_amount',f.total_amount::text,
            'previous_recognition_date',f.recognition_date::text,
            'line_no',m.line_no,'journal_id',m.journal_id,'net_amount',m.net_amount::text,
            'tax_amount',m.tax_amount::text,'total_amount',m.total_amount::text,
            'recognition_date',m.recognition_date::text) ORDER BY m.member_no)
        INTO invalid,members_json FROM members m JOIN matches x ON x.id=m.id
        LEFT JOIN facts f ON f.journal_id=m.previous_journal_id;
    IF invalid THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='Credit revision response is inconsistent',
            CONSTRAINT='ck_credit_revision_shape';
    END IF;
    RETURN jsonb_build_object(
        'kind','business_credit_revision','credit_id',entry.credit_id,'revision_id',entry.id,
        'ledger_id',entry.ledger_id,'document_id',entry.document_id,'asset_id',document.asset_id,
        'previous_version',entry.group_version-1,'version',entry.group_version,
        'action',entry.action,'reason',entry.revision_reason,
        'previous_credit_date',prior_date::text,'previous_credit_reason',prior_reason,
        'credit_date',entry.credit_date::text,'replacement_reason',entry.credit_reason,
        'members',members_json);
END; $$;
"""

_RECEIPT = r"""
CREATE FUNCTION coinpup_validate_credit_revision_receipt(revision_identifier uuid) RETURNS void
LANGUAGE plpgsql SECURITY INVOKER SET search_path=pg_catalog AS $$
DECLARE entry public.business_credit_revisions%ROWTYPE; receipt public.command_receipts%ROWTYPE;
    anchor uuid;
BEGIN
    SELECT * INTO entry FROM public.business_credit_revisions WHERE id=revision_identifier;
    SELECT * INTO receipt FROM public.command_receipts
        WHERE ledger_id=entry.ledger_id AND key=entry.idempotency_key;
    SELECT operation_id INTO anchor FROM public.business_credit_revision_members
        WHERE revision_id=entry.id AND member_no=1;
    IF entry.id IS NULL OR anchor IS NULL OR receipt.hash_version IS DISTINCT FROM 2
       OR receipt.response_status IS DISTINCT FROM 200
       OR receipt.operation_id IS DISTINCT FROM anchor THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='Credit revision receipt is inconsistent',
            CONSTRAINT='ck_credit_revision_shape';
    END IF;
    -- Canonical JSONB text also distinguishes integer contract fields from 2.0 numeric values.
    IF receipt.response::text IS DISTINCT FROM
       public.coinpup_business_credit_revision_response(entry.id)::text THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='Credit revision receipt is inconsistent',
            CONSTRAINT='ck_credit_revision_shape';
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
        "LOCK TABLE public.business_credit_revisions, "
        "public.business_credit_revision_members IN ACCESS EXCLUSIVE MODE"
    )
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM public.business_credit_revisions)
               OR EXISTS (SELECT 1 FROM public.business_credit_revision_members)
               OR EXISTS (SELECT 1 FROM public.change_log WHERE entity_type IN
                   ('business_credit_revision_members','business_credit_revisions')
                   OR (entity_type='business_credits' AND entity_version>1)) THEN
                RAISE EXCEPTION USING ERRCODE='23514',
                    MESSAGE='Credit revision history prevents downgrade',
                    CONSTRAINT='ck_credit_revision_downgrade';
            END IF;
        END; $$;
    """)
    op.execute("DROP FUNCTION coinpup_validate_credit_revision_receipt(uuid)")
    op.execute("DROP FUNCTION coinpup_business_credit_revision_response(uuid)")
