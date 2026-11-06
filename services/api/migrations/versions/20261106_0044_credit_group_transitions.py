"""Prove complete credit transitions without opening revision writes or replacing callers."""

from alembic import op

revision = "20261106_0044"
down_revision = "20261106_0043"
branch_labels = None
depends_on = None

_TRANSITIONS = r"""
CREATE FUNCTION coinpup_validate_credit_group_transitions(credit_identifier uuid) RETURNS void
LANGUAGE plpgsql SECURITY INVOKER SET search_path=pg_catalog AS $$
DECLARE entry public.business_credits%ROWTYPE; step record; prior_count integer;
    member_count integer; next_count integer; original_count integer; expected_kind text;
BEGIN
    SELECT * INTO entry FROM public.business_credits WHERE id=credit_identifier;
    SELECT CASE document_kind WHEN 'invoice' THEN 'invoice_credit' WHEN 'bill' THEN 'bill_credit'
        END INTO expected_kind FROM public.business_documents
        WHERE id=entry.document_id AND ledger_id=entry.ledger_id;
    SELECT count(*) INTO original_count FROM public.business_credit_lines WHERE credit_id=entry.id;
    IF entry.id IS NULL OR expected_kind IS NULL OR original_count NOT BETWEEN 1 AND 200
       OR (SELECT max(line_no) FROM public.business_credit_lines WHERE credit_id=entry.id)
          IS DISTINCT FROM original_count OR EXISTS (
        SELECT 1 FROM (
            SELECT r.*, lag(id) OVER w AS prior_id, lag(group_version::bigint) OVER w AS prior_v,
                lag(action) OVER w AS prior_action FROM public.business_credit_revisions r
            WHERE r.credit_id=entry.id WINDOW w AS (ORDER BY group_version)
        ) h WHERE h.group_version::bigint <> coalesce(h.prior_v,1)+1
            OR h.previous_revision_id IS DISTINCT FROM h.prior_id OR h.prior_action='cancel'
    ) THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='Credit revision chain is inconsistent',
            CONSTRAINT='ck_credit_revision_shape';
    END IF;
    FOR step IN SELECT * FROM public.business_credit_revisions
        WHERE credit_id=entry.id ORDER BY group_version LOOP
        SELECT count(*) INTO prior_count FROM public.coinpup_credit_posting_facts(
            entry.ledger_id,ARRAY[entry.id],NULL) f
            WHERE f.group_version::bigint=step.group_version::bigint-1;
        SELECT count(*), count(*) FILTER (WHERE action<>'cancel') INTO member_count,next_count
            FROM public.business_credit_revision_members WHERE revision_id=step.id;
        IF prior_count NOT BETWEEN 1 AND 200 OR member_count NOT BETWEEN 1 AND 400
           OR (step.action='correct' AND next_count NOT BETWEEN 1 AND 200)
           OR (step.action='cancel' AND next_count<>0)
           OR (SELECT max(member_no) FROM public.business_credit_revision_members
                WHERE revision_id=step.id) IS DISTINCT FROM member_count
           OR (next_count>0 AND (SELECT max(line_no) FROM public.business_credit_revision_members
                WHERE revision_id=step.id) IS DISTINCT FROM next_count)
           OR EXISTS (
                SELECT 1 FROM (
                    SELECT member_no, row_number() OVER (ORDER BY (action='cancel'),
                        CASE WHEN action='cancel' THEN previous_line_no ELSE line_no END) AS n
                    FROM public.business_credit_revision_members WHERE revision_id=step.id
                ) ordered WHERE member_no<>n
           ) OR EXISTS (
                WITH previous AS (
                    SELECT * FROM public.coinpup_credit_posting_facts(
                        entry.ledger_id,ARRAY[entry.id],NULL) f
                    WHERE f.group_version::bigint=step.group_version::bigint-1
                ), members AS (
                    SELECT * FROM public.business_credit_revision_members WHERE revision_id=step.id
                )
                SELECT 1 FROM previous p FULL JOIN members m USING (source_line_id)
                WHERE m.id IS NULL OR (m.action='create' AND p.journal_id IS NOT NULL)
                   OR (m.action<>'create' AND (p.journal_id IS NULL OR
                       ROW(m.source_operation_id,m.operation_id,m.previous_line_no,
                           m.previous_operation_version,m.previous_journal_id)
                       IS DISTINCT FROM ROW(p.source_operation_id,p.operation_id,p.line_no,
                           p.operation_version,p.journal_id)))
           ) THEN
            RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='Credit membership is incomplete',
                CONSTRAINT='ck_credit_revision_shape';
        END IF;
        IF EXISTS (
            SELECT 1 FROM public.business_credit_revision_members m
            LEFT JOIN public.journals p ON p.id=m.previous_journal_id
            LEFT JOIN public.journals r ON r.id=m.reversal_journal_id
            LEFT JOIN public.journals n ON n.id=m.journal_id
            WHERE m.revision_id=step.id AND (
                (m.action<>'create' AND (
                    ROW(p.ledger_id,p.operation_id,p.operation_version,p.journal_kind)
                        IS DISTINCT FROM ROW(entry.ledger_id,m.operation_id,
                            m.previous_operation_version,'posting'::varchar)
                    OR ROW(r.ledger_id,r.operation_id,r.operation_version,r.journal_kind,
                        r.reverses_journal_id,r.revision_reason)
                        IS DISTINCT FROM ROW(entry.ledger_id,m.operation_id,m.operation_version,
                            'reversal'::varchar,m.previous_journal_id,step.revision_reason)))
                OR (m.action<>'cancel' AND (
                    ROW(n.ledger_id,n.operation_id,n.operation_version,n.journal_kind,
                        n.reverses_journal_id,n.revision_reason)
                        IS DISTINCT FROM ROW(entry.ledger_id,m.operation_id,m.operation_version,
                            'posting'::varchar,NULL::uuid,
                            CASE WHEN m.action='correct' THEN step.revision_reason ELSE NULL END)))
            )
        ) THEN
            RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='Credit transition journals are invalid',
                CONSTRAINT='ck_credit_revision_shape';
        END IF;
    END LOOP;
    IF EXISTS (
        WITH owned AS (
            SELECT operation_id FROM public.business_credit_lines WHERE credit_id=entry.id
            UNION SELECT operation_id FROM public.business_credit_revision_members
                WHERE credit_id=entry.id
        ), appearances AS (
            SELECT l.operation_id,l.credit_id,l.document_id,l.ledger_id,l.source_line_id,1 AS birth
            FROM public.business_credit_lines l JOIN owned o USING (operation_id)
            UNION ALL
            SELECT m.operation_id,m.credit_id,m.document_id,m.ledger_id,m.source_line_id,
                (m.action='create')::integer FROM public.business_credit_revision_members m
                JOIN owned o USING (operation_id)
        )
        SELECT 1 FROM appearances GROUP BY operation_id
        HAVING count(DISTINCT ROW(credit_id,document_id,ledger_id,source_line_id))<>1
            OR sum(birth)<>1 OR NOT bool_and(credit_id=entry.id AND ledger_id=entry.ledger_id)
    ) THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='Credit operation ownership is invalid',
            CONSTRAINT='ck_credit_revision_shape';
    END IF;
    IF EXISTS (
        WITH events AS (
            SELECT l.operation_id,1 AS group_version,1 AS operation_version,
                'create'::text AS action,
                l.journal_id,NULL::uuid AS previous_journal_id,entry.created_by AS created_by
            FROM public.business_credit_lines l WHERE l.credit_id=entry.id
            UNION ALL
            SELECT m.operation_id,r.group_version,m.operation_version,m.action::text,
                m.journal_id,m.previous_journal_id,r.created_by
            FROM public.business_credit_revision_members m
            JOIN public.business_credit_revisions r ON r.id=m.revision_id
            WHERE m.credit_id=entry.id
        ), latest AS (
            SELECT DISTINCT ON (operation_id) * FROM events ORDER BY operation_id,group_version DESC
        ), births AS (SELECT * FROM events WHERE action='create')
        SELECT 1 FROM latest e JOIN births b USING (operation_id)
        LEFT JOIN public.financial_operations o ON o.id=e.operation_id
        WHERE ROW(o.ledger_id,o.kind,o.created_by,o.version,o.status,o.current_journal_id)
            IS DISTINCT FROM ROW(entry.ledger_id,expected_kind::varchar,b.created_by,
                e.operation_version,
                CASE WHEN e.action='cancel' THEN 'cancelled'::varchar ELSE 'active'::varchar END,
                CASE WHEN e.action='cancel' THEN e.previous_journal_id ELSE e.journal_id END)
    ) THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='Credit operation state is inconsistent',
            CONSTRAINT='ck_credit_revision_shape';
    END IF;
END; $$;
"""


def upgrade():
    op.execute("SELECT pg_advisory_xact_lock(18945999704708432)")
    op.execute(_TRANSITIONS)


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
    op.execute("DROP FUNCTION coinpup_validate_credit_group_transitions(uuid)")
