"""Use current return quantities for the database's payment and document capacity checks."""

from pathlib import Path

from alembic import op

revision = "20261109_0064"
down_revision = "20261109_0063"
branch_labels = None
depends_on = None

# Canonical PostgreSQL 17 definitions of the three 0063 validators, preserved verbatim.
_FROZEN = Path(__file__).resolve().parent / "sql" / "0064"

_TOTALS = r"""
CREATE FUNCTION coinpup_returned_payment_amount(ledger_identifier uuid, payment_identifier uuid)
RETURNS numeric LANGUAGE sql STABLE STRICT SECURITY INVOKER SET search_path=pg_catalog AS $$
    SELECT coalesce(sum(r.payment_amount),0)
    FROM public.coinpup_current_return_members(ledger_identifier, ARRAY(
        SELECT a.id FROM public.business_allocation_lines a
        WHERE a.ledger_id=ledger_identifier AND a.payment_id=payment_identifier
    )) r;
$$;

CREATE FUNCTION coinpup_returned_document_amount(
    ledger_identifier uuid, document_line_identifier uuid
)
RETURNS numeric LANGUAGE sql STABLE STRICT SECURITY INVOKER SET search_path=pg_catalog AS $$
    SELECT coalesce(sum(r.document_amount),0)
    FROM public.coinpup_current_return_members(ledger_identifier, ARRAY(
        SELECT a.id FROM public.business_allocation_lines a
        WHERE a.ledger_id=ledger_identifier AND a.document_line_id=document_line_identifier
    )) r;
$$;
"""

_OLD_ALLOCATION = (_FROZEN / "allocation.sql").read_text(encoding="utf-8")

_ALLOCATION = _OLD_ALLOCATION.replace(
    (
        r"""(SELECT coalesce(sum(r.payment_amount),0)
             FROM public.business_allocation_return_lines r
             WHERE r.payment_id=payment.id AND r.ledger_id=allocation.ledger_id)"""
    ),
    "public.coinpup_returned_payment_amount(allocation.ledger_id,payment.id)",
).replace(
    (
        r"""(SELECT coalesce(sum(r.document_amount),0)
                 FROM public.business_allocation_return_lines r
                 JOIN public.business_allocation_lines a
                   ON a.id=r.source_allocation_line_id AND a.ledger_id=r.ledger_id
                 WHERE a.document_line_id=item.document_line_id
                   AND a.ledger_id=allocation.ledger_id)"""
    ),
    "public.coinpup_returned_document_amount(allocation.ledger_id,item.document_line_id)",
)

_OLD_CREDIT = (_FROZEN / "credit.sql").read_text(encoding="utf-8")

_CREDIT = _OLD_CREDIT.replace(
    (
        r"""(SELECT coalesce(sum(r.document_amount),0)
              FROM public.business_allocation_return_lines r
              JOIN public.business_allocation_lines a
                ON a.id=r.source_allocation_line_id AND a.ledger_id=r.ledger_id
              WHERE a.document_line_id=item.id AND a.ledger_id=entry.ledger_id)"""
    ),
    "public.coinpup_returned_document_amount(entry.ledger_id,item.id)",
)

_OLD_REFUND = (_FROZEN / "refund.sql").read_text(encoding="utf-8")

_REFUND = _OLD_REFUND.replace(
    (
        r"""(SELECT coalesce(sum(r.payment_amount),0)
             FROM public.business_allocation_return_lines r
             WHERE r.payment_id=payment.id AND r.ledger_id=entry.ledger_id)"""
    ),
    "public.coinpup_returned_payment_amount(entry.ledger_id,payment.id)",
)


def upgrade():
    op.execute("SELECT pg_advisory_xact_lock(18945999704708432)")
    op.execute(_TOTALS)
    op.execute(_ALLOCATION)
    op.execute(_CREDIT)
    op.execute(_REFUND)


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
    op.execute(_OLD_REFUND)
    op.execute(_OLD_CREDIT)
    op.execute(_OLD_ALLOCATION)
    op.execute("DROP FUNCTION coinpup_returned_document_amount(uuid,uuid)")
    op.execute("DROP FUNCTION coinpup_returned_payment_amount(uuid,uuid)")
