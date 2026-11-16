"""Check refund posting dates against immutable evidence versions instead of the original date."""

from pathlib import Path

from alembic import op

revision = "20261109_0071"
down_revision = "20261109_0070"
branch_labels = None
depends_on = None


_MATCH = r"""
CREATE FUNCTION coinpup_refund_evidence_at_date(ledger_identifier uuid,payment_identifier uuid,
    refund_identifier uuid,revision_identifier uuid,requested uuid[],refund_date date)
RETURNS boolean LANGUAGE sql STABLE SECURITY INVOKER SET search_path=pg_catalog AS $$
    WITH evidence AS (
        SELECT e.return_id,e.return_version,e.line_no FROM public.business_refund_returns e
        WHERE revision_identifier IS NULL AND (e.refund_id,e.payment_id,e.ledger_id)
            =(refund_identifier,payment_identifier,ledger_identifier)
        UNION ALL
        SELECT e.return_id,e.return_version,e.line_no FROM public.business_refund_revision_returns e
        WHERE e.revision_id=revision_identifier AND (e.refund_id,e.payment_id,e.ledger_id)
            =(refund_identifier,payment_identifier,ledger_identifier)
    ), dates AS (
        SELECT e.*,row_number() OVER (ORDER BY e.line_no) AS position,
               public.coinpup_bound_return_date(ledger_identifier,payment_identifier,
                   e.return_id,e.return_version) AS bound_date FROM evidence e
    )
    SELECT requested IS NOT NULL AND refund_date IS NOT NULL AND cardinality(requested)<=200
      AND EXISTS (SELECT 1 FROM public.business_refunds f
          WHERE (f.id,f.payment_id,f.ledger_id)=(refund_identifier,payment_identifier,
              ledger_identifier))
      AND (revision_identifier IS NULL OR EXISTS (SELECT 1 FROM public.business_refund_revisions h
          WHERE (h.id,h.refund_id,h.payment_id,h.ledger_id)=(revision_identifier,
              refund_identifier,payment_identifier,ledger_identifier) AND h.action='correct'))
      AND coalesce(array_agg(return_id ORDER BY line_no),ARRAY[]::uuid[])
          IS NOT DISTINCT FROM requested
      AND coalesce(bool_and(bound_date IS NOT NULL AND bound_date<=refund_date
          AND position=line_no),true)
    FROM dates;
$$;
"""

# Freeze the actual PostgreSQL 17 predecessor; all cash legs, fees and dimensions stay identical.
_OLD_POSTING = (Path(__file__).resolve().parent / "sql" / "0071" / "refund_posting.sql").read_text(
    encoding="utf-8"
)
_POSTING = _OLD_POSTING.replace(
    r"""EXISTS (SELECT 1 FROM unnest(fact.allocation_return_ids) AS x(return_id)
           LEFT JOIN public.business_allocation_returns r ON r.id=x.return_id
              AND r.payment_id=fact.payment_id AND r.ledger_id=fact.ledger_id
           WHERE r.id IS NULL OR r.return_date>fact.transaction_date)""",
    """NOT public.coinpup_refund_evidence_at_date(fact.ledger_id,fact.payment_id,
           fact.refund_id,fact.revision_id,fact.allocation_return_ids,fact.transaction_date)""",
)


def upgrade():
    op.execute("SELECT pg_advisory_xact_lock(18945999704708432)")
    op.execute(_MATCH)
    op.execute(_POSTING)


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
    op.execute(_OLD_POSTING)
    op.execute("DROP FUNCTION coinpup_refund_evidence_at_date(uuid,uuid,uuid,uuid,uuid[],date)")
