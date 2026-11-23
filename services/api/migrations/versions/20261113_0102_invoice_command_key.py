"""Keep a pending Invoice key exclusive with existing permanent financial receipts."""

from alembic import op

revision = "20261113_0102"
down_revision = "20261113_0101"
branch_labels = None
depends_on = None

_KEY = r"""
CREATE FUNCTION coinpup_guard_invoice_command_key() RETURNS trigger
LANGUAGE plpgsql SET search_path=pg_catalog AS $$
DECLARE conflict boolean;
BEGIN
    PERFORM pg_advisory_xact_lock(18945999704708432);
    IF current_setting('transaction_isolation') <> 'read committed' THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='Invalid command-key write isolation',
            CONSTRAINT='ck_invoice_command_key_isolation';
    END IF;
    IF TG_TABLE_NAME='business_invoice_reservations' THEN
        SELECT EXISTS(SELECT 1 FROM public.command_receipts
            WHERE (ledger_id,key)=(NEW.ledger_id,NEW.idempotency_key)) INTO conflict;
    ELSE
        SELECT EXISTS(SELECT 1 FROM public.business_invoice_reservations
            WHERE (ledger_id,idempotency_key)=(NEW.ledger_id,NEW.key)) INTO conflict;
    END IF;
    IF conflict THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='The command key is already reserved',
            CONSTRAINT='ck_invoice_command_key';
    END IF;
    RETURN NEW;
END; $$;
"""


def upgrade():
    op.execute("SELECT pg_advisory_xact_lock(18945999704708432)")
    op.execute(_KEY)
    for table in ("business_invoice_reservations", "command_receipts"):
        op.execute(
            f"CREATE TRIGGER tr_invoice_command_key BEFORE INSERT ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION coinpup_guard_invoice_command_key()"
        )


def downgrade():
    op.execute("SELECT pg_advisory_xact_lock(18945999704708432)")
    op.execute("LOCK TABLE business_invoice_reservations IN ACCESS EXCLUSIVE MODE")
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM public.business_invoice_reservations) OR EXISTS (
                SELECT 1 FROM public.change_log WHERE entity_type='business_invoice_reservations'
            ) THEN
                RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='Invoice key history exists',
                    CONSTRAINT='ck_invoice_command_key_downgrade';
            END IF;
        END; $$;
    """)
    for table in ("business_invoice_reservations", "command_receipts"):
        op.execute(f"DROP TRIGGER tr_invoice_command_key ON {table}")
    op.execute("DROP FUNCTION coinpup_guard_invoice_command_key()")
