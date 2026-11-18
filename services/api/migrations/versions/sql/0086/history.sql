CREATE OR REPLACE FUNCTION public.coinpup_validate_return_source_history(ledger_identifier uuid, return_identifier uuid, revision_identifier uuid)
 RETURNS void
 LANGUAGE plpgsql
 STABLE
 SET search_path TO 'pg_catalog'
AS $function$
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
END; $function$
