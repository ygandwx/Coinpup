"""Bind every new refund evidence row to the active return version under the ledger write lock."""

from alembic import op

revision = "20261109_0070"
down_revision = "20261109_0069"
branch_labels = None
depends_on = None


_OLD_REFERENCES = r"""
CREATE OR REPLACE FUNCTION coinpup_current_refund_return_references(
    ledger_identifier uuid, payment_identifier uuid, return_identifiers uuid[]
)
RETURNS TABLE (
    return_id uuid, return_version integer, refund_id uuid, refund_version integer,
    revision_id uuid, line_no integer, refund_date date
) LANGUAGE sql STABLE STRICT SECURITY INVOKER SET search_path=pg_catalog AS $$
    WITH candidates AS (
        SELECT e.refund_id FROM public.business_refund_returns e
        WHERE e.ledger_id=ledger_identifier AND e.payment_id=payment_identifier
          AND e.return_id=ANY(return_identifiers)
        UNION
        SELECT e.refund_id FROM public.business_refund_revision_returns e
        WHERE e.ledger_id=ledger_identifier AND e.payment_id=payment_identifier
          AND e.return_id=ANY(return_identifiers)
    ), current_refunds AS (
        SELECT f.id AS refund_id, f.transaction_date AS original_date, v.id AS revision_id,
               v.operation_version, v.action, v.transaction_date
        FROM candidates c
        JOIN public.business_refunds f ON f.id=c.refund_id
            AND f.ledger_id=ledger_identifier AND f.payment_id=payment_identifier
        LEFT JOIN LATERAL (
            SELECT h.id, h.operation_version, h.action, h.transaction_date
            FROM public.business_refund_revisions h
            WHERE h.refund_id=f.id AND h.ledger_id=f.ledger_id AND h.payment_id=f.payment_id
            ORDER BY h.operation_version DESC LIMIT 1
        ) v ON true
    )
    SELECT e.return_id, 1, c.refund_id, 1, NULL::uuid, e.line_no, c.original_date
    FROM current_refunds c JOIN public.business_refund_returns e ON e.refund_id=c.refund_id
    WHERE c.revision_id IS NULL AND e.ledger_id=ledger_identifier
      AND e.payment_id=payment_identifier AND e.return_id=ANY(return_identifiers)
    UNION ALL
    SELECT e.return_id, e.return_version, c.refund_id, c.operation_version, c.revision_id,
           e.line_no, c.transaction_date
    FROM current_refunds c JOIN public.business_refund_revision_returns e
      ON e.revision_id=c.revision_id AND e.refund_id=c.refund_id
    WHERE c.action='correct' AND e.ledger_id=ledger_identifier
      AND e.payment_id=payment_identifier AND e.return_id=ANY(return_identifiers);
$$;
"""
_REFERENCES = _OLD_REFERENCES.replace(
    "SELECT e.return_id, 1, c.refund_id", "SELECT e.return_id, e.return_version, c.refund_id"
)

_EVIDENCE = r"""
CREATE FUNCTION coinpup_current_return_evidence(ledger_identifier uuid,payment_identifier uuid,
    return_identifiers uuid[])
RETURNS TABLE (return_id uuid,return_version integer,return_date date)
LANGUAGE sql STABLE STRICT SECURITY INVOKER SET search_path=pg_catalog AS $$
    SELECT g.id,coalesce(v.group_version,1),
           CASE WHEN v.id IS NULL THEN g.return_date ELSE v.return_date END
    FROM public.business_allocation_returns g LEFT JOIN LATERAL (
        SELECT r.id,r.group_version,r.action,r.return_date FROM public.business_return_revisions r
        WHERE (r.return_id,r.payment_id,r.ledger_id)=(g.id,g.payment_id,g.ledger_id)
        ORDER BY r.group_version DESC LIMIT 1
    ) v ON true
    WHERE g.ledger_id=ledger_identifier AND g.payment_id=payment_identifier
      AND g.id=ANY(return_identifiers) AND (v.id IS NULL OR v.action='correct');
$$;

CREATE FUNCTION coinpup_bound_return_date(ledger_identifier uuid,payment_identifier uuid,
    return_identifier uuid,version_identifier integer) RETURNS date
LANGUAGE sql STABLE STRICT SECURITY INVOKER SET search_path=pg_catalog AS $$
    SELECT g.return_date FROM public.business_allocation_returns g
    WHERE version_identifier=1 AND (g.id,g.payment_id,g.ledger_id)
        =(return_identifier,payment_identifier,ledger_identifier)
    UNION ALL
    SELECT r.return_date FROM public.business_return_revisions r
    WHERE version_identifier>1 AND r.group_version=version_identifier AND r.action='correct'
      AND (r.return_id,r.payment_id,r.ledger_id)=(return_identifier,payment_identifier,
          ledger_identifier);
$$;

CREATE FUNCTION coinpup_bind_refund_return_version() RETURNS trigger
LANGUAGE plpgsql SECURITY INVOKER SET search_path=pg_catalog AS $$
DECLARE effective integer;
BEGIN
    PERFORM pg_advisory_xact_lock(18945999704708432);
    -- The existing scoped FK handles absent and foreign groups with its original error.
    IF NOT EXISTS (SELECT 1 FROM public.business_allocation_returns
        WHERE (id,payment_id,ledger_id)=(NEW.return_id,NEW.payment_id,NEW.ledger_id)) THEN
        RETURN NEW;
    END IF;
    SELECT e.return_version INTO effective FROM public.coinpup_current_return_evidence(
        NEW.ledger_id,NEW.payment_id,ARRAY[NEW.return_id]) e;
    IF effective IS NULL OR effective IS DISTINCT FROM NEW.return_version THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='Refund return evidence is not current',
            CONSTRAINT=TG_ARGV[0];
    END IF;
    RETURN NEW;
END; $$;
"""


def upgrade():
    op.execute("SELECT pg_advisory_xact_lock(18945999704708432)")
    op.execute("""
        ALTER TABLE public.business_refund_returns
            ADD COLUMN return_version integer NOT NULL DEFAULT 1,
            ADD CONSTRAINT ck_business_refund_return_source_version CHECK (return_version>=1);
        ALTER TABLE public.business_refund_revision_returns
            DROP CONSTRAINT ck_refund_revision_return_source_version,
            ADD CONSTRAINT ck_refund_revision_return_source_version CHECK (return_version>=1);
    """)
    op.execute(_EVIDENCE)
    op.execute(_REFERENCES)
    op.execute("""
        CREATE TRIGGER tr_bind_original_refund_evidence BEFORE INSERT
            ON public.business_refund_returns
            FOR EACH ROW EXECUTE FUNCTION public.coinpup_bind_refund_return_version(
                'ck_business_refund_return_source_version');
        CREATE TRIGGER tr_bind_revised_refund_evidence BEFORE INSERT
            ON public.business_refund_revision_returns FOR EACH ROW
            EXECUTE FUNCTION public.coinpup_bind_refund_return_version(
                'ck_refund_revision_return_source_version');
    """)


def downgrade():
    op.execute("SELECT pg_advisory_xact_lock(18945999704708432)")
    op.execute("""
        LOCK TABLE public.business_refund_returns,public.business_refund_revision_returns,
            public.business_return_revisions,public.business_return_revision_members
            IN ACCESS EXCLUSIVE MODE;
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM public.business_return_revisions)
                OR EXISTS (SELECT 1 FROM public.business_return_revision_members)
                OR EXISTS (SELECT 1 FROM public.business_refund_returns WHERE return_version<>1)
                OR EXISTS (SELECT 1 FROM public.business_refund_revision_returns
                    WHERE return_version<>1)
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
    op.execute(_OLD_REFERENCES)
    op.execute("""
        DROP TRIGGER tr_bind_revised_refund_evidence ON public.business_refund_revision_returns;
        DROP TRIGGER tr_bind_original_refund_evidence ON public.business_refund_returns;
        DROP FUNCTION public.coinpup_bind_refund_return_version();
        DROP FUNCTION public.coinpup_current_return_evidence(uuid,uuid,uuid[]);
        DROP FUNCTION public.coinpup_bound_return_date(uuid,uuid,uuid,integer);
        ALTER TABLE public.business_refund_revision_returns
            DROP CONSTRAINT ck_refund_revision_return_source_version,
            ADD CONSTRAINT ck_refund_revision_return_source_version CHECK (return_version=1);
        ALTER TABLE public.business_refund_returns
            DROP CONSTRAINT ck_business_refund_return_source_version, DROP COLUMN return_version;
    """)
