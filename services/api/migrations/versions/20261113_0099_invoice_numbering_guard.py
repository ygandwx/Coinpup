"""Enforce company numbering order under the existing lock; complete reservation stays closed."""

from alembic import op

revision = "20261113_0099"
down_revision = "20261113_0098"
branch_labels = None
depends_on = None

_OLD_GUARD = r"""
CREATE OR REPLACE FUNCTION coinpup_guard_invoice_reservation() RETURNS trigger
LANGUAGE plpgsql SET search_path=pg_catalog AS $$
BEGIN
    IF TG_OP <> 'INSERT' THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='Invoice reservation history is immutable',
            CONSTRAINT='ck_invoice_reservation_immutable';
    END IF;
    PERFORM pg_advisory_xact_lock(18945999704708432);
    IF current_setting('transaction_isolation') <> 'read committed' THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='Invalid Invoice reservation isolation',
            CONSTRAINT='ck_invoice_reservation_isolation';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM public.ledgers l JOIN public.entities e ON e.id=l.entity_id
        WHERE l.id=NEW.ledger_id AND e.kind='company') THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='Invoice reservation requires a company',
            CONSTRAINT='ck_invoice_reservation_company';
    END IF;
    RETURN NEW;
END; $$;

"""

_GUARD = r"""
CREATE OR REPLACE FUNCTION coinpup_guard_invoice_reservation() RETURNS trigger
LANGUAGE plpgsql SET search_path=pg_catalog AS $$
DECLARE prefix text; digits integer; last_sequence bigint; expected_number text;
BEGIN
    IF TG_OP <> 'INSERT' THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='Invoice reservation history is immutable',
            CONSTRAINT='ck_invoice_reservation_immutable';
    END IF;
    PERFORM pg_advisory_xact_lock(18945999704708432);
    IF current_setting('transaction_isolation') <> 'read committed' THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='Invalid Invoice reservation isolation',
            CONSTRAINT='ck_invoice_reservation_isolation';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM public.ledgers l JOIN public.entities e ON e.id=l.entity_id
        WHERE l.id=NEW.ledger_id AND e.kind='company') THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='Invoice reservation requires a company',
            CONSTRAINT='ck_invoice_reservation_company';
    END IF;
    SELECT p.number_prefix,p.minimum_digits INTO prefix,digits
    FROM public.business_invoice_profiles p
    JOIN public.ledgers l ON l.id=p.ledger_id
    JOIN public.entities e ON e.id=l.entity_id
    WHERE (p.id,p.ledger_id,p.version)=
        (NEW.configuration_id,NEW.ledger_id,NEW.configuration_version)
        AND NOT p.archived AND NOT e.archived;
    IF NOT FOUND THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='Invoice numbering review is stale',
            CONSTRAINT='ck_invoice_numbering_configuration';
    END IF;
    SELECT COALESCE(max(r.sequence),0) INTO last_sequence
    FROM public.business_invoice_reservations r WHERE r.ledger_id=NEW.ledger_id;
    IF last_sequence=9223372036854775807 THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='Invoice sequence is exhausted',
            CONSTRAINT='ck_invoice_numbering_exhausted';
    END IF;
    IF NEW.sequence <> last_sequence+1 THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='Invoice sequence must advance once',
            CONSTRAINT='ck_invoice_numbering_sequence';
    END IF;
    expected_number := prefix || repeat('0',GREATEST(digits-length(NEW.sequence::text),0))
        || NEW.sequence::text;
    IF NEW.number IS DISTINCT FROM expected_number THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='Invoice number must match reviewed policy',
            CONSTRAINT='ck_invoice_numbering_policy';
    END IF;
    RETURN NEW;
END; $$;

"""


def upgrade():
    op.execute("SELECT pg_advisory_xact_lock(18945999704708432)")
    op.execute(_GUARD)


def downgrade():
    op.execute("SELECT pg_advisory_xact_lock(18945999704708432)")
    op.execute("LOCK TABLE business_invoice_reservations IN ACCESS EXCLUSIVE MODE")
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM public.business_invoice_reservations) OR EXISTS (
                SELECT 1 FROM public.change_log WHERE entity_type='business_invoice_reservations'
            ) THEN
                RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='Invoice numbering history exists',
                    CONSTRAINT='ck_invoice_numbering_downgrade';
            END IF;
        END; $$;
    """)
    op.execute(_OLD_GUARD)
