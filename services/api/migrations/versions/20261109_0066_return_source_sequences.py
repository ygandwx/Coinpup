"""Verify permanent return positions and the effective rounding sequence for each source."""

from alembic import op

revision = "20261109_0066"
down_revision = "20261109_0065"
branch_labels = None
depends_on = None


_SLOTS = r"""
CREATE FUNCTION coinpup_return_source_slots(ledger_identifier uuid, source_identifiers uuid[])
RETURNS TABLE (source_id uuid, return_id uuid, operation_id uuid, source_return_no bigint,
    revision_id uuid, declared_predecessor uuid, birth_seq bigint, birth_events bigint,
    assigned_no bigint, predecessor uuid)
LANGUAGE sql STABLE STRICT SECURITY INVOKER SET search_path=pg_catalog AS $$
    WITH births AS (
        SELECT l.source_allocation_line_id AS source_id, l.return_id, l.operation_id,
               l.source_return_no, NULL::uuid AS revision_id, NULL::uuid AS declared_predecessor,
               'business_allocation_return_lines' AS event_type, l.id AS event_id
        FROM public.business_allocation_return_lines l
        WHERE l.ledger_id=ledger_identifier AND l.source_allocation_line_id=ANY(source_identifiers)
        UNION ALL
        SELECT m.source_allocation_line_id, m.return_id, m.operation_id, m.source_return_no,
               m.revision_id, m.source_predecessor_operation_id,
               'business_return_revision_members', m.id
        FROM public.business_return_revision_members m
        WHERE m.ledger_id=ledger_identifier AND m.source_allocation_line_id=ANY(source_identifiers)
          AND m.action='create'
    ), recorded AS (
        SELECT b.*, e.birth_seq, e.birth_events FROM births b
        CROSS JOIN LATERAL (
            SELECT min(log.seq) AS birth_seq, count(*) AS birth_events
            FROM public.change_log log WHERE log.ledger_id=ledger_identifier
              AND log.entity_type IN
                  ('business_allocation_return_lines','business_return_revision_members')
              AND log.entity_version=1 AND log.entity_type=b.event_type
              AND log.entity_id=b.event_id::text
        ) e
    )
    SELECT b.source_id,b.return_id,b.operation_id,b.source_return_no,b.revision_id,
           b.declared_predecessor,b.birth_seq,b.birth_events,
           row_number() OVER slots,lag(b.operation_id) OVER slots
    FROM recorded b WINDOW slots AS (PARTITION BY b.source_id ORDER BY b.birth_seq,b.event_id);
$$;
"""

_SHARE = r"""
CREATE FUNCTION coinpup_return_share_valid(source_p numeric, source_d numeric,
    prior_p numeric, prior_d numeric, share_p numeric, share_d numeric, same_asset boolean)
RETURNS boolean LANGUAGE sql IMMUTABLE STRICT PARALLEL SAFE SET search_path=pg_catalog AS $$
    SELECT CASE WHEN source_p<=0 OR source_d<=0 OR prior_p<0 OR prior_d<0
        OR share_p<=0 OR share_d<=0 OR (prior_p=0)<>(prior_d=0)
        OR trunc(source_p)<>source_p OR trunc(source_d)<>source_d
        OR trunc(prior_p)<>prior_p OR trunc(prior_d)<>prior_d
        OR trunc(share_p)<>share_p OR trunc(share_d)<>share_d
        OR (same_asset AND (source_p<>source_d OR prior_p<>prior_d OR share_p<>share_d))
        OR prior_p+share_p>source_p OR prior_d+share_d>source_d
        OR (prior_p+share_p=source_p)<>(prior_d+share_d=source_d) THEN false
        WHEN prior_d+share_d=source_d THEN share_p=source_p-prior_p
        ELSE share_p=div(2*share_d*source_p+source_d,2*source_d) END;
$$;
"""

_CHECK = r"""
CREATE FUNCTION coinpup_validate_return_source_sequences(ledger_identifier uuid,
    source_identifiers uuid[]) RETURNS void
LANGUAGE plpgsql STABLE SECURITY INVOKER SET search_path=pg_catalog AS $$
BEGIN
    IF ledger_identifier IS NULL OR coalesce(cardinality(source_identifiers),0)=0 OR EXISTS (
        SELECT 1 FROM unnest(source_identifiers) AS input(id)
        WHERE NOT EXISTS (SELECT 1 FROM public.business_allocation_lines a
            WHERE (a.id,a.ledger_id)=(input.id,ledger_identifier))
    ) THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='Return source sequence is invalid',
            CONSTRAINT='ck_return_revision_source_sequence';
    END IF;
    IF EXISTS (
        WITH slots AS MATERIALIZED (
            SELECT * FROM public.coinpup_return_source_slots(ledger_identifier,source_identifiers)
        )
        SELECT 1 FROM slots b WHERE b.birth_events<>1 OR b.assigned_no<>b.source_return_no
          OR (b.revision_id IS NOT NULL AND (b.declared_predecessor IS DISTINCT FROM b.predecessor
            OR EXISTS (SELECT 1 FROM public.business_allocation_return_lines original
                WHERE original.operation_id=b.operation_id)))
        UNION ALL
        SELECT 1 FROM public.business_return_revision_members m LEFT JOIN slots b
          ON (b.source_id,b.return_id,b.operation_id,b.source_return_no)=
             (m.source_allocation_line_id,m.return_id,m.operation_id,m.source_return_no)
        WHERE m.ledger_id=ledger_identifier AND m.source_allocation_line_id=ANY(source_identifiers)
          AND (b.operation_id IS NULL OR m.source_predecessor_operation_id
              IS DISTINCT FROM b.predecessor)
    ) THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='Return source sequence is invalid',
            CONSTRAINT='ck_return_revision_source_sequence';
    END IF;
    IF EXISTS (
        WITH shares AS (
            SELECT r.source_allocation_line_id AS source_id,r.source_return_no,r.return_id,
                   p.asset_id=d.asset_id AS same_asset,
                   a.payment_amount*(10::numeric^pa.scale) AS source_p,
                   a.document_amount*(10::numeric^da.scale) AS source_d,
                   r.payment_amount*(10::numeric^pa.scale) AS share_p,
                   r.document_amount*(10::numeric^da.scale) AS share_d
            FROM public.coinpup_current_return_members(ledger_identifier,source_identifiers) r
            JOIN public.business_allocation_lines a
              ON (a.id,a.payment_id,a.ledger_id)
                =(r.source_allocation_line_id,r.payment_id,r.ledger_id)
            JOIN public.business_payments p ON (p.id,p.ledger_id)=(a.payment_id,a.ledger_id)
            JOIN public.business_documents d ON (d.id,d.ledger_id)=(a.document_id,a.ledger_id)
            JOIN public.assets pa ON pa.asset_id=p.asset_id
            JOIN public.assets da ON da.asset_id=d.asset_id
        ), ordered AS (
            SELECT s.*, coalesce(sum(share_p) OVER earlier,0) AS prior_p,
                   coalesce(sum(share_d) OVER earlier,0) AS prior_d,
                   count(*) OVER (PARTITION BY source_id,return_id) AS group_slots
            FROM shares s WINDOW earlier AS (PARTITION BY source_id ORDER BY source_return_no
                ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING)
        )
        SELECT 1 FROM ordered WHERE group_slots<>1 OR public.coinpup_return_share_valid(
            source_p,source_d,prior_p,prior_d,share_p,share_d,same_asset) IS DISTINCT FROM true
    ) THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='Return shares violate the source ratio',
            CONSTRAINT='ck_return_revision_source_sequence';
    END IF;
END; $$;
"""


def upgrade():
    op.execute("SELECT pg_advisory_xact_lock(18945999704708432)")
    op.execute("""
        CREATE INDEX ix_change_log_return_slot_birth ON public.change_log
            (ledger_id,entity_type,entity_id) INCLUDE (seq)
            WHERE entity_version=1 AND entity_type IN
                ('business_allocation_return_lines','business_return_revision_members')
    """)
    op.execute(_SLOTS)
    op.execute(_SHARE)
    op.execute(_CHECK)


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
    op.execute("DROP FUNCTION coinpup_validate_return_source_sequences(uuid,uuid[])")
    op.execute(
        "DROP FUNCTION coinpup_return_share_valid(numeric,numeric,numeric,numeric,"
        "numeric,numeric,boolean)"
    )
    op.execute("DROP FUNCTION coinpup_return_source_slots(uuid,uuid[])")
    op.execute("DROP INDEX public.ix_change_log_return_slot_birth")
