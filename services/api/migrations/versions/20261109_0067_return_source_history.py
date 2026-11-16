"""Prove stored return prefixes and all surviving source shares at a specific write."""

from alembic import op

revision = "20261109_0067"
down_revision = "20261109_0066"
branch_labels = None
depends_on = None


_EVENTS = r"""
CREATE FUNCTION coinpup_return_source_events(ledger_identifier uuid, source_identifiers uuid[])
RETURNS TABLE (source_id uuid, return_id uuid, revision_id uuid, event_id uuid,
    source_no bigint, operation_id uuid, operation_version integer, action text,
    payment_amount numeric, document_amount numeric, prefix_p numeric, prefix_d numeric,
    absorbs boolean, event_seq bigint, event_count bigint)
LANGUAGE sql STABLE STRICT SECURITY INVOKER SET search_path=pg_catalog AS $$
    WITH facts AS (
        SELECT l.source_allocation_line_id AS source_id,l.return_id,NULL::uuid AS revision_id,
               l.id AS event_id,l.source_return_no,l.operation_id,1 AS operation_version,
               'create' AS action,l.payment_amount,l.document_amount,
               NULL::numeric AS prefix_p,NULL::numeric AS prefix_d,NULL::boolean AS absorbs,
               'business_allocation_return_lines' AS event_type
        FROM public.business_allocation_return_lines l
        WHERE l.ledger_id=ledger_identifier AND l.source_allocation_line_id=ANY(source_identifiers)
        UNION ALL
        SELECT m.source_allocation_line_id,m.return_id,m.revision_id,m.id,m.source_return_no,
               m.operation_id,m.operation_version,m.action::text,m.payment_amount,m.document_amount,
               m.prefix_payment_amount,m.prefix_document_amount,m.absorbs_remainder,
               'business_return_revision_members'
        FROM public.business_return_revision_members m
        WHERE m.ledger_id=ledger_identifier AND m.source_allocation_line_id=ANY(source_identifiers)
    )
    SELECT f.source_id,f.return_id,f.revision_id,f.event_id,f.source_return_no,f.operation_id,
           f.operation_version,f.action,f.payment_amount,f.document_amount,f.prefix_p,f.prefix_d,
           f.absorbs,recorded.event_seq,recorded.event_count
    FROM facts f CROSS JOIN LATERAL (
        SELECT min(log.seq) AS event_seq,count(*) AS event_count FROM public.change_log log
        WHERE log.ledger_id=ledger_identifier AND log.entity_version=1
          AND log.entity_type IN
              ('business_allocation_return_lines','business_return_revision_members')
          AND log.entity_type=f.event_type AND log.entity_id=f.event_id::text
    ) recorded;
$$;
"""

_HISTORY = r"""
CREATE FUNCTION coinpup_validate_return_source_history(ledger_identifier uuid,
    return_identifier uuid, revision_identifier uuid) RETURNS void
LANGUAGE plpgsql STABLE SECURITY INVOKER SET search_path=pg_catalog AS $$
DECLARE sources uuid[];
BEGIN
    IF revision_identifier IS NULL THEN
        SELECT array_agg(l.source_allocation_line_id) INTO sources
        FROM public.business_allocation_return_lines l
        WHERE l.ledger_id=ledger_identifier AND l.return_id=return_identifier;
    ELSE
        SELECT array_agg(m.source_allocation_line_id) INTO sources
        FROM public.business_return_revision_members m
        WHERE m.ledger_id=ledger_identifier AND m.return_id=return_identifier
          AND m.revision_id=revision_identifier;
    END IF;
    IF coalesce(cardinality(sources),0) NOT BETWEEN 1 AND 400
       OR (revision_identifier IS NULL AND cardinality(sources)>200) OR EXISTS (
        WITH events AS MATERIALIZED (
            SELECT * FROM public.coinpup_return_source_events(ledger_identifier,sources)
        ), targets AS (
            SELECT e.* FROM events e WHERE e.return_id=return_identifier
              AND e.revision_id IS NOT DISTINCT FROM revision_identifier
        ), history AS (
            SELECT t.event_id AS target,t.event_seq AS at_seq,e.*,
                   row_number() OVER lifetime AS expected_version,
                   first_value(e.return_id) OVER lifetime AS born_group,
                   first_value(e.source_no) OVER lifetime AS born_slot,
                   first_value(e.event_seq) OVER lifetime AS born_seq,
                   lag(e.action) OVER lifetime AS prior_action
            FROM targets t JOIN events e ON e.source_id=t.source_id AND e.event_seq<=t.event_seq
            WINDOW lifetime AS (PARTITION BY t.event_id,e.operation_id ORDER BY e.event_seq)
        ), state AS (
            SELECT DISTINCT ON (h.target,h.operation_id) h.*
            FROM history h ORDER BY h.target,h.operation_id,h.event_seq DESC
        ), prefixes AS (
            SELECT s.*,row_number() OVER birth_order AS reserved_no,
                   count(*) FILTER (WHERE s.action<>'cancel')
                      OVER (PARTITION BY s.target,s.return_id) AS active_in_group,
                   coalesce(sum(s.payment_amount) OVER preceding_slots,0) AS earlier_p,
                   coalesce(sum(s.document_amount) OVER preceding_slots,0) AS earlier_d
            FROM state s
            WINDOW birth_order AS (PARTITION BY s.target ORDER BY s.born_seq),
                   preceding_slots AS (PARTITION BY s.target ORDER BY s.source_no
                       ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING)
        ), amounts AS (
            SELECT e.*,a.payment_amount AS source_p,a.document_amount AS source_d,
                   10::numeric^pa.scale AS p_units,10::numeric^da.scale AS d_units,
                   p.asset_id=d.asset_id AS same_asset
            FROM prefixes e JOIN public.business_allocation_lines a
              ON (a.id,a.ledger_id)=(e.source_id,ledger_identifier)
            JOIN public.business_payments p ON (p.id,p.ledger_id)=(a.payment_id,a.ledger_id)
            JOIN public.business_documents d ON (d.id,d.ledger_id)=(a.document_id,a.ledger_id)
            JOIN public.assets pa ON pa.asset_id=p.asset_id
            JOIN public.assets da ON da.asset_id=d.asset_id
        )
        SELECT 1 FROM events WHERE event_count<>1
        UNION ALL
        SELECT 1 FROM history WHERE event_count<>1 OR operation_version<>expected_version
          OR (action='create')<>(expected_version=1) OR prior_action='cancel'
          OR return_id<>born_group OR source_no<>born_slot
        UNION ALL
        SELECT 1 FROM amounts WHERE reserved_no<>source_no OR active_in_group>1
          OR (action<>'cancel' AND public.coinpup_return_share_valid(
                source_p*p_units,source_d*d_units,earlier_p*p_units,earlier_d*d_units,
                payment_amount*p_units,document_amount*d_units,same_asset) IS DISTINCT FROM true)
          OR (event_seq=at_seq AND revision_id IS NOT NULL
              AND (prefix_p IS DISTINCT FROM earlier_p OR prefix_d IS DISTINCT FROM earlier_d
                   OR absorbs IS DISTINCT FROM (CASE WHEN action<>'cancel'
                      THEN earlier_d+document_amount=source_d END)))
    ) THEN
        RAISE EXCEPTION USING ERRCODE='23514',
            MESSAGE='Allocation return source history is invalid',
            CONSTRAINT='ck_return_revision_source_history';
    END IF;
END; $$;
"""


def upgrade():
    op.execute("SELECT pg_advisory_xact_lock(18945999704708432)")
    op.execute(_EVENTS)
    op.execute(_HISTORY)


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
    op.execute("DROP FUNCTION coinpup_validate_return_source_history(uuid,uuid,uuid)")
    op.execute("DROP FUNCTION coinpup_return_source_events(uuid,uuid[])")
