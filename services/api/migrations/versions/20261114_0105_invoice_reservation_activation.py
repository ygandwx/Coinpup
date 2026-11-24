"""Allow a fully validated Invoice reservation to remain pending after commit."""

from alembic import op

revision = "20261114_0105"
down_revision = "20261114_0104"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("SELECT pg_advisory_xact_lock(18945999704708432)")
    # 0099-0103 enforce the number, reviewed facts, key and chain at INSERT.
    # The original AFTER trigger records the pending reservation in this transaction.
    # The staging trigger is the only one removed; pending does not require PDFs.
    op.execute(
        "DROP TRIGGER tr_invoice_reservation_complete ON public.business_invoice_reservations"
    )


def downgrade():
    op.execute("SELECT pg_advisory_xact_lock(18945999704708432)")
    op.execute(
        "LOCK TABLE public.business_invoice_reservations, public.business_invoice_completions "
        "IN ACCESS EXCLUSIVE MODE"
    )
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM public.business_invoice_reservations)
                OR EXISTS (SELECT 1 FROM public.business_invoice_completions)
                OR EXISTS (SELECT 1 FROM public.change_log WHERE entity_type IN
                    ('business_invoice_reservations','business_invoice_completions')) THEN
                RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='Invoice issuance history exists',
                    CONSTRAINT='ck_invoice_activation_downgrade';
            END IF;
        END; $$;
    """)
    op.execute(
        "CREATE CONSTRAINT TRIGGER tr_invoice_reservation_complete AFTER INSERT "
        "ON public.business_invoice_reservations DEFERRABLE INITIALLY DEFERRED "
        "FOR EACH ROW EXECUTE FUNCTION public.coinpup_check_invoice_reservation()"
    )
