"""Prepare stable credit notifications and ownership checks while revision writes stay closed."""

from alembic import op

revision = "20261106_0047"
down_revision = "20261106_0046"
branch_labels = None
depends_on = None

_TRIGGERS = r"""
CREATE FUNCTION coinpup_check_credit_operation_group() RETURNS trigger
LANGUAGE plpgsql SET search_path=pg_catalog AS $$
DECLARE operation public.financial_operations%ROWTYPE; credit_groups uuid[];
BEGIN
    -- Earlier queued NEW versions are not the final database state.
    SELECT * INTO operation FROM public.financial_operations WHERE id=NEW.id;
    IF operation.kind NOT IN ('invoice_credit','bill_credit') THEN RETURN NULL; END IF;
    SELECT array_agg(credit_id) INTO credit_groups FROM (
        SELECT credit_id FROM public.business_credit_lines WHERE operation_id=operation.id
        UNION SELECT credit_id FROM public.business_credit_revision_members
            WHERE operation_id=operation.id
    ) owned;
    IF coalesce(cardinality(credit_groups),0)<>1 THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='Credit operation ownership is invalid',
            CONSTRAINT='ck_credit_revision_shape';
    END IF;
    PERFORM public.coinpup_validate_business_credit(credit_groups[1]);
    RETURN NULL;
END; $$;

CREATE FUNCTION coinpup_record_credit_group_change() RETURNS trigger
LANGUAGE plpgsql SET search_path=pg_catalog AS $$
DECLARE owner_identifier uuid;
BEGIN
    -- Acquire the global writer lock before the identity sequence is evaluated.
    PERFORM pg_advisory_xact_lock(18945999704708432);
    SELECT owner_id INTO owner_identifier FROM public.ledgers WHERE id=NEW.ledger_id;
    INSERT INTO public.change_log
        (owner_id,ledger_id,entity_type,entity_id,entity_version,change_kind)
    VALUES (owner_identifier,NEW.ledger_id,'business_credits',NEW.credit_id::text,
        NEW.group_version,CASE WHEN NEW.action='cancel' THEN 'cancel' ELSE 'upsert' END);
    RETURN NEW;
END; $$;
"""


def upgrade():
    op.execute("SELECT pg_advisory_xact_lock(18945999704708432)")
    op.execute(_TRIGGERS)
    op.execute(
        "CREATE CONSTRAINT TRIGGER zz_tr_credit_operations_complete AFTER INSERT OR UPDATE "
        "ON financial_operations DEFERRABLE INITIALLY DEFERRED FOR EACH ROW "
        "WHEN (NEW.kind IN ('invoice_credit','bill_credit')) "
        "EXECUTE FUNCTION coinpup_check_credit_operation_group()"
    )
    op.execute(
        "CREATE TRIGGER trg_business_credit_group_change_log AFTER INSERT "
        "ON business_credit_revisions FOR EACH ROW "
        "EXECUTE FUNCTION coinpup_record_credit_group_change()"
    )


def downgrade():
    op.execute("SELECT pg_advisory_xact_lock(18945999704708432)")
    op.execute(
        "LOCK TABLE public.business_credit_revisions, "
        "public.business_credit_revision_members IN ACCESS EXCLUSIVE MODE"
    )
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM public.business_credit_revisions)
               OR EXISTS (SELECT 1 FROM public.business_credit_revision_members)
               OR EXISTS (SELECT 1 FROM public.change_log WHERE entity_type IN
                   ('business_credit_revision_members','business_credit_revisions')
                   OR (entity_type='business_credits' AND entity_version>1)) THEN
                RAISE EXCEPTION USING ERRCODE='23514',
                    MESSAGE='Credit revision history prevents downgrade',
                    CONSTRAINT='ck_credit_revision_downgrade';
            END IF;
        END; $$;
    """)
    op.execute("DROP TRIGGER zz_tr_credit_operations_complete ON financial_operations")
    op.execute("DROP TRIGGER trg_business_credit_group_change_log ON business_credit_revisions")
    op.execute("DROP FUNCTION coinpup_check_credit_operation_group()")
    op.execute("DROP FUNCTION coinpup_record_credit_group_change()")
