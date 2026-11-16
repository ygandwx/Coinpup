"""Commit complete return groups with permanent receipts, active capacities and source history."""

from pathlib import Path

from alembic import op

revision = "20261109_0074"
down_revision = "20261109_0073"
branch_labels = None
depends_on = None


_FROZEN = Path(__file__).resolve().parent / "sql" / "0074"
_PREDECESSORS = (
    "coinpup_check_allocation_return",
    "coinpup_check_return_revision",
    "coinpup_check_return_revision_member",
    "coinpup_guard_return_revision",
    "coinpup_validate_business_allocation_return",
    "coinpup_validate_return_revision",
    "coinpup_validate_shape_unallocation",
)

_ORIGIN = r"""
CREATE OR REPLACE FUNCTION coinpup_validate_business_allocation_return(return_identifier uuid)
RETURNS void LANGUAGE plpgsql SET search_path=pg_catalog AS $$
DECLARE origin public.business_allocation_returns%ROWTYPE; sources uuid[]; problem text;
BEGIN
    PERFORM public.coinpup_validate_return_origin_facts(return_identifier);
    SELECT * INTO origin FROM public.business_allocation_returns WHERE id=return_identifier;
    SELECT array_agg(l.source_allocation_line_id) INTO sources
    FROM public.business_allocation_return_lines l WHERE l.return_id=return_identifier;
    PERFORM public.coinpup_validate_return_operation_owner(l.operation_id)
        FROM public.business_allocation_return_lines l WHERE l.return_id=return_identifier;
    BEGIN
        PERFORM public.coinpup_validate_return_source_history(origin.ledger_id,origin.id,NULL);
        PERFORM public.coinpup_validate_return_source_sequences(origin.ledger_id,sources);
    EXCEPTION WHEN check_violation THEN
        GET STACKED DIAGNOSTICS problem=CONSTRAINT_NAME;
        IF problem IN ('ck_return_revision_source_sequence','ck_return_revision_source_history')
        THEN
            RAISE EXCEPTION USING ERRCODE='23514',
                MESSAGE='Allocation return exceeds its original mapping',
                CONSTRAINT='ck_business_allocation_return_shape';
        END IF;
        RAISE;
    END;
    PERFORM public.coinpup_validate_return_capacity(origin.ledger_id,origin.payment_id,sources);
END; $$;
"""

_REVISION = r"""
CREATE OR REPLACE FUNCTION coinpup_validate_return_revision(revision_identifier uuid) RETURNS void
LANGUAGE plpgsql SET search_path=pg_catalog AS $$
DECLARE h public.business_return_revisions%ROWTYPE; sources uuid[];
BEGIN
    PERFORM public.coinpup_validate_return_transition(revision_identifier);
    SELECT * INTO h FROM public.business_return_revisions WHERE id=revision_identifier;
    SELECT array_agg(m.source_allocation_line_id) INTO sources
    FROM public.business_return_revision_members m WHERE m.revision_id=revision_identifier;
    PERFORM public.coinpup_validate_return_source_history(h.ledger_id,h.return_id,h.id);
    PERFORM public.coinpup_validate_return_source_sequences(h.ledger_id,sources);
    PERFORM public.coinpup_validate_return_capacity(h.ledger_id,h.payment_id,sources);
END; $$;
"""

_SHAPE = r"""
CREATE OR REPLACE FUNCTION coinpup_validate_shape_unallocation(journal_identifier uuid,
    common_result jsonb) RETURNS void LANGUAGE plpgsql SET search_path=pg_catalog AS $$
BEGIN
    PERFORM public.coinpup_validate_return_posting_fact(journal_identifier,common_result);
END; $$;
"""

# At header validation all contiguous slots are covered by an immutable receipt. After that
# no existing row or receipt can change. The per-group unique ordinal rejects all occupied
# slots; a later append has no recorded slot. Always check this independently on child inserts,
# including after a caller has already fired the header constraint with SET CONSTRAINTS.
_MEMBERS = r"""
CREATE OR REPLACE FUNCTION coinpup_check_allocation_return() RETURNS trigger
LANGUAGE plpgsql SET search_path=pg_catalog AS $$
DECLARE recorded text;
BEGIN
    IF TG_TABLE_NAME='business_allocation_returns' THEN
        PERFORM public.coinpup_validate_business_allocation_return(NEW.id);
    ELSE
        SELECT r.response->'postings'->(NEW.line_no-1)->>'operation_id' INTO recorded
        FROM public.business_allocation_returns h JOIN public.command_receipts r
          ON (r.ledger_id,r.key)=(h.ledger_id,h.idempotency_key)
        WHERE (h.id,h.ledger_id,h.payment_id)=(NEW.return_id,NEW.ledger_id,NEW.payment_id);
        IF recorded IS DISTINCT FROM NEW.operation_id::text THEN
            RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='Return member lacks its fixed 201 slot',
                CONSTRAINT='ck_business_allocation_return_shape';
        END IF;
    END IF;
    RETURN NULL;
END; $$;

CREATE OR REPLACE FUNCTION coinpup_check_return_revision_member() RETURNS trigger
LANGUAGE plpgsql SET search_path=pg_catalog AS $$
DECLARE recorded text;
BEGIN
    SELECT r.response->'members'->(NEW.member_no-1)->>'operation_id' INTO recorded
    FROM public.business_return_revisions h JOIN public.command_receipts r
      ON (r.ledger_id,r.key)=(h.ledger_id,h.idempotency_key)
    WHERE (h.id,h.return_id,h.ledger_id,h.payment_id)
      =(NEW.revision_id,NEW.return_id,NEW.ledger_id,NEW.payment_id);
    IF recorded IS DISTINCT FROM NEW.operation_id::text THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='Return member lacks its fixed 200 slot',
            CONSTRAINT='ck_return_revision_shape';
    END IF;
    RETURN NULL;
END; $$;
"""

# The immediate guard sees the effective refund references before a new header is recorded.
# The existing deferred guard still proves them against the transaction's final state.
_DEPENDENCIES = r"""
CREATE FUNCTION coinpup_require_return_refund_dates(ledger_identifier uuid, payment_identifier uuid,
    return_identifier uuid, change_action varchar, changed_date date) RETURNS void
LANGUAGE plpgsql STABLE SECURITY INVOKER SET search_path=pg_catalog AS $$
DECLARE earliest_refund date;
BEGIN
    SELECT min(e.refund_date) INTO earliest_refund
    FROM public.coinpup_current_refund_return_references(
        ledger_identifier,payment_identifier,ARRAY[return_identifier]) e;
    IF earliest_refund IS NOT NULL AND change_action='cancel' THEN
        RAISE EXCEPTION USING ERRCODE='23514',
            MESSAGE='Correct each effective refund reference before cancelling the return',
            CONSTRAINT='ck_return_revision_refund_reference';
    END IF;
    IF earliest_refund IS NOT NULL AND changed_date>earliest_refund THEN
        RAISE EXCEPTION USING ERRCODE='23514',
            MESSAGE='A referenced return cannot follow the effective refund date',
            CONSTRAINT='ck_return_revision_refund_date';
    END IF;
END; $$;
"""
_CHECK_REFS = (
    "    PERFORM public.coinpup_require_return_refund_dates(NEW.ledger_id,NEW.payment_id,\n"
    "        NEW.return_id,NEW.action,NEW.return_date);\n"
)
_OLD_GUARD = (_FROZEN / "coinpup_guard_return_revision.sql").read_text(encoding="utf-8")
_GUARD = _OLD_GUARD.replace(
    "    RETURN NEW;",
    "    IF TG_TABLE_NAME='business_return_revisions' THEN\n"
    + _CHECK_REFS
    + "    END IF;\n    RETURN NEW;",
)

_CHECK = (
    r"""
CREATE OR REPLACE FUNCTION coinpup_check_return_revision() RETURNS trigger
LANGUAGE plpgsql SET search_path=pg_catalog AS $$
BEGIN
"""
    + _CHECK_REFS
    + r"""
    PERFORM public.coinpup_validate_return_revision(NEW.id);
    RETURN NULL;
END; $$;
"""
)


def upgrade():
    op.execute("SELECT pg_advisory_xact_lock(18945999704708432)")
    for definition in (_ORIGIN, _REVISION, _SHAPE, _MEMBERS, _DEPENDENCIES, _GUARD, _CHECK):
        op.execute(definition)


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
    for name in _PREDECESSORS:
        op.execute((_FROZEN / f"{name}.sql").read_text(encoding="utf-8"))
    op.execute("DROP FUNCTION coinpup_require_return_refund_dates(uuid,uuid,uuid,varchar,date)")
