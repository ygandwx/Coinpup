"""Require an explicit refund evidence change before cancelling a referenced return."""

from alembic import op

revision = "20261109_0060"
down_revision = "20261109_0059"
branch_labels = None
depends_on = None


_OLD_CHECK = r"""
CREATE OR REPLACE FUNCTION coinpup_check_return_revision() RETURNS trigger
LANGUAGE plpgsql SET search_path=pg_catalog AS $$
BEGIN
    PERFORM public.coinpup_validate_return_revision(NEW.id);
    RETURN NULL;
END; $$;
"""

_CHECK = r"""
CREATE OR REPLACE FUNCTION coinpup_check_return_revision() RETURNS trigger
LANGUAGE plpgsql SET search_path=pg_catalog AS $$
DECLARE earliest_refund date;
BEGIN
    SELECT min(e.refund_date) INTO earliest_refund
    FROM public.coinpup_current_refund_return_references(
        NEW.ledger_id, NEW.payment_id, ARRAY[NEW.return_id]
    ) e;
    IF earliest_refund IS NOT NULL THEN
        IF NEW.action='cancel' THEN
            RAISE EXCEPTION USING ERRCODE='23514',
                MESSAGE='Correct each effective refund reference before cancelling the return',
                CONSTRAINT='ck_return_revision_refund_reference';
        END IF;
        IF NEW.return_date > earliest_refund THEN
            RAISE EXCEPTION USING ERRCODE='23514',
                MESSAGE='A referenced return cannot follow the effective refund date',
                CONSTRAINT='ck_return_revision_refund_date';
        END IF;
    END IF;
    PERFORM public.coinpup_validate_return_revision(NEW.id);
    RETURN NULL;
END; $$;
"""


def upgrade():
    op.execute("SELECT pg_advisory_xact_lock(18945999704708432)")
    op.execute(_CHECK)


def downgrade():
    op.execute("SELECT pg_advisory_xact_lock(18945999704708432)")
    op.execute("LOCK TABLE public.business_return_revisions IN ACCESS EXCLUSIVE MODE")
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM public.business_return_revisions)
               OR EXISTS (SELECT 1 FROM public.change_log
                   WHERE entity_type='business_return_revisions'
                      OR (entity_type='business_allocation_returns' AND entity_version>1)) THEN
                RAISE EXCEPTION USING ERRCODE='23514',
                    MESSAGE='Allocation return revision history prevents downgrade',
                    CONSTRAINT='ck_return_revision_downgrade';
            END IF;
        END; $$;
    """)
    op.execute(_OLD_CHECK)
