"""Tie every unallocation operation and stable group notification to its immutable return."""

from alembic import op

revision = "20261109_0069"
down_revision = "20261109_0068"
branch_labels = None
depends_on = None


_GUARDS = r"""
CREATE FUNCTION coinpup_validate_return_operation_owner(operation_identifier uuid) RETURNS void
LANGUAGE plpgsql STABLE SECURITY INVOKER SET search_path=pg_catalog AS $$
DECLARE o public.financial_operations%ROWTYPE; event record; group_id uuid; source_id uuid;
    reserved_no bigint; last_version bigint:=0; last_journal uuid; closed boolean:=false;
BEGIN
    SELECT * INTO o FROM public.financial_operations WHERE id=operation_identifier;
    IF o.kind IS DISTINCT FROM 'unallocation' THEN
        RAISE EXCEPTION USING ERRCODE='23514',
                MESSAGE='Allocation return ownership is invalid',
            CONSTRAINT='ck_return_revision_shape';
    END IF;
    -- The global constraint can fire once per operation. Restrict this proof to that operation;
    -- the group constraint is responsible for the complete set of members and receipts.
    FOR event IN
        SELECT l.return_id,l.source_allocation_line_id,l.source_return_no,l.ledger_id,l.payment_id,
               1 AS operation_version,'create' AS action,NULL::uuid AS previous_journal_id,
               l.journal_id,h.created_by
        FROM public.business_allocation_return_lines l JOIN public.business_allocation_returns h
          ON (h.id,h.payment_id,h.ledger_id)=(l.return_id,l.payment_id,l.ledger_id)
        WHERE l.operation_id=o.id
        UNION ALL
        SELECT m.return_id,m.source_allocation_line_id,m.source_return_no,m.ledger_id,m.payment_id,
               m.operation_version,m.action,m.previous_journal_id,m.journal_id,h.created_by
        FROM public.business_return_revision_members m JOIN public.business_allocation_returns h
          ON (h.id,h.payment_id,h.ledger_id)=(m.return_id,m.payment_id,m.ledger_id)
        WHERE m.operation_id=o.id
        ORDER BY operation_version
    LOOP
        IF last_version=0 THEN
            group_id:=event.return_id; source_id:=event.source_allocation_line_id;
            reserved_no:=event.source_return_no;
        END IF;
        IF closed OR event.operation_version::bigint<>last_version+1
           OR event.return_id<>group_id OR event.source_allocation_line_id<>source_id
           OR event.source_return_no<>reserved_no OR event.ledger_id<>o.ledger_id
           OR event.created_by<>o.created_by OR (event.action='create')<>(last_version=0)
           OR event.previous_journal_id IS DISTINCT FROM last_journal THEN
            RAISE EXCEPTION USING ERRCODE='23514',
                MESSAGE='Allocation return ownership is invalid',
                CONSTRAINT='ck_return_revision_shape';
        END IF;
        last_version:=event.operation_version;
        closed:=event.action='cancel';
        IF NOT closed THEN last_journal:=event.journal_id; END IF;
    END LOOP;
    IF last_version=0 OR o.version::bigint<>last_version
       OR o.current_journal_id IS DISTINCT FROM last_journal
       OR o.status IS DISTINCT FROM (CASE WHEN closed THEN 'cancelled' ELSE 'active' END) THEN
        RAISE EXCEPTION USING ERRCODE='23514',
                MESSAGE='Allocation return ownership is invalid',
            CONSTRAINT='ck_return_revision_shape';
    END IF;
END; $$;

CREATE FUNCTION coinpup_check_return_operation_owner() RETURNS trigger
LANGUAGE plpgsql SECURITY INVOKER SET search_path=pg_catalog AS $$
BEGIN
    -- Always read final state, even for an earlier deferred INSERT or UPDATE event.
    PERFORM public.coinpup_validate_return_operation_owner(NEW.id);
    RETURN NULL;
END; $$;

CREATE FUNCTION coinpup_record_return_group_change() RETURNS trigger
LANGUAGE plpgsql SECURITY INVOKER SET search_path=pg_catalog AS $$
DECLARE owner_identifier uuid;
BEGIN
    PERFORM pg_advisory_xact_lock(18945999704708432);
    SELECT owner_id INTO owner_identifier FROM public.ledgers WHERE id=NEW.ledger_id;
    INSERT INTO public.change_log
        (owner_id,ledger_id,entity_type,entity_id,entity_version,change_kind)
    VALUES (owner_identifier,NEW.ledger_id,'business_allocation_returns',NEW.return_id::text,
        NEW.group_version,CASE WHEN NEW.action='cancel' THEN 'cancel' ELSE 'upsert' END);
    RETURN NEW;
END; $$;
"""


def upgrade():
    op.execute("SELECT pg_advisory_xact_lock(18945999704708432)")
    op.execute(_GUARDS)
    op.execute(
        "CREATE CONSTRAINT TRIGGER zz_tr_return_operations_complete AFTER INSERT OR UPDATE "
        "ON financial_operations DEFERRABLE INITIALLY DEFERRED FOR EACH ROW "
        "WHEN (NEW.kind='unallocation') EXECUTE FUNCTION coinpup_check_return_operation_owner()"
    )
    op.execute(
        "CREATE TRIGGER trg_business_return_group_change_log AFTER INSERT "
        "ON business_return_revisions FOR EACH ROW "
        "EXECUTE FUNCTION coinpup_record_return_group_change()"
    )


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
    op.execute("DROP TRIGGER zz_tr_return_operations_complete ON financial_operations")
    op.execute("DROP TRIGGER trg_business_return_group_change_log ON business_return_revisions")
    op.execute("DROP FUNCTION coinpup_check_return_operation_owner()")
    op.execute("DROP FUNCTION coinpup_validate_return_operation_owner(uuid)")
    op.execute("DROP FUNCTION coinpup_record_return_group_change()")
