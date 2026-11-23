"""New reviewed versions extend one immutable document reservation chain."""

from alembic import op

revision = "20261114_0103"
down_revision = "20261113_0102"
branch_labels = None
depends_on = None

_PREVIOUS = r"""
CREATE FUNCTION coinpup_guard_invoice_predecessor() RETURNS trigger
LANGUAGE plpgsql SET search_path=pg_catalog AS $$
DECLARE previous public.business_invoice_reservations%ROWTYPE;
BEGIN
    SELECT * INTO previous FROM public.business_invoice_reservations
        WHERE (ledger_id,document_id)=(NEW.ledger_id,NEW.document_id)
        ORDER BY sequence DESC LIMIT 1;
    IF NEW.previous_issuance_id IS DISTINCT FROM previous.id
       OR (previous.id IS NOT NULL AND (previous.sequence>=NEW.sequence
           OR previous.document_version>=NEW.document_version)) THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='Invoice predecessor review is stale',
            CONSTRAINT='ck_invoice_predecessor';
    END IF;
    RETURN NEW;
END; $$;
"""


def upgrade():
    op.execute("SELECT pg_advisory_xact_lock(18945999704708432)")
    op.execute(_PREVIOUS)
    # The key/number guards have taken the global write lock, and both snapshot
    # guards have proved the current document before this chain comparison.
    op.execute(
        "CREATE TRIGGER tr_invoice_snapshot_previous BEFORE INSERT "
        "ON business_invoice_reservations FOR EACH ROW "
        "EXECUTE FUNCTION coinpup_guard_invoice_predecessor()"
    )


def downgrade():
    op.execute("SELECT pg_advisory_xact_lock(18945999704708432)")
    op.execute("LOCK TABLE business_invoice_reservations IN ACCESS EXCLUSIVE MODE")
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM public.business_invoice_reservations) OR EXISTS (
                SELECT 1 FROM public.change_log WHERE entity_type='business_invoice_reservations'
            ) THEN
                RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='Invoice predecessor history exists',
                    CONSTRAINT='ck_invoice_predecessor_downgrade';
            END IF;
        END; $$;
    """)
    op.execute("DROP TRIGGER tr_invoice_snapshot_previous ON business_invoice_reservations")
    op.execute("DROP FUNCTION coinpup_guard_invoice_predecessor()")
