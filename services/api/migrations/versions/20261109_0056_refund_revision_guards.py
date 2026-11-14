"""Tie refund operation heads and stable change notifications to immutable refund owners."""

from alembic import op

revision = "20261109_0056"
down_revision = "20261109_0055"
branch_labels = None
depends_on = None


_GUARDS = r"""
CREATE FUNCTION coinpup_check_refund_operation_owner() RETURNS trigger
LANGUAGE plpgsql SECURITY INVOKER SET search_path=pg_catalog AS $$
DECLARE refund_identifier uuid;
BEGIN
    -- The deferred check validates the final stored operation, not the queued version.
    SELECT f.id INTO refund_identifier FROM public.business_refunds f
        WHERE f.ledger_id=NEW.ledger_id AND f.operation_id=NEW.id;
    IF NOT FOUND THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='Refund operation ownership is invalid',
            CONSTRAINT='ck_refund_revision_shape';
    END IF;
    PERFORM public.coinpup_validate_business_refund(refund_identifier);
    RETURN NULL;
END; $$;

CREATE FUNCTION coinpup_record_refund_revision_change() RETURNS trigger
LANGUAGE plpgsql SECURITY INVOKER SET search_path=pg_catalog AS $$
DECLARE owner_identifier uuid;
BEGIN
    PERFORM pg_advisory_xact_lock(18945999704708432);
    SELECT owner_id INTO owner_identifier FROM public.ledgers WHERE id=NEW.ledger_id;
    INSERT INTO public.change_log
        (owner_id,ledger_id,entity_type,entity_id,entity_version,change_kind)
    VALUES (owner_identifier,NEW.ledger_id,'business_refunds',NEW.refund_id::text,
        NEW.operation_version,CASE WHEN NEW.action='cancel' THEN 'cancel' ELSE 'upsert' END);
    RETURN NEW;
END; $$;
"""


def upgrade():
    op.execute("SELECT pg_advisory_xact_lock(18945999704708432)")
    op.execute(_GUARDS)
    op.execute(
        "CREATE CONSTRAINT TRIGGER zz_tr_refund_operations_complete AFTER INSERT OR UPDATE "
        "ON financial_operations DEFERRABLE INITIALLY DEFERRED FOR EACH ROW "
        "WHEN (NEW.kind='refund') EXECUTE FUNCTION coinpup_check_refund_operation_owner()"
    )
    op.execute(
        "CREATE TRIGGER trg_business_refund_revision_change_log AFTER INSERT "
        "ON business_refund_revisions FOR EACH ROW "
        "EXECUTE FUNCTION coinpup_record_refund_revision_change()"
    )


def downgrade():
    op.execute("SELECT pg_advisory_xact_lock(18945999704708432)")
    op.execute(
        "LOCK TABLE public.business_refund_revisions, public.business_refund_revision_returns "
        "IN ACCESS EXCLUSIVE MODE"
    )
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM public.business_refund_revisions)
                OR EXISTS (SELECT 1 FROM public.business_refund_revision_returns)
                OR EXISTS (SELECT 1 FROM public.change_log WHERE
                    entity_type IN ('business_refund_revisions','business_refund_revision_returns')
                    OR (entity_type='business_refunds' AND entity_version>1)) THEN
                RAISE EXCEPTION USING ERRCODE='23514',
                    MESSAGE='Refund revision history prevents downgrade',
                    CONSTRAINT='ck_refund_revision_downgrade';
            END IF;
        END; $$;
    """)
    op.execute("DROP TRIGGER zz_tr_refund_operations_complete ON financial_operations")
    op.execute("DROP TRIGGER trg_business_refund_revision_change_log ON business_refund_revisions")
    op.execute("DROP FUNCTION coinpup_check_refund_operation_owner()")
    op.execute("DROP FUNCTION coinpup_record_refund_revision_change()")
