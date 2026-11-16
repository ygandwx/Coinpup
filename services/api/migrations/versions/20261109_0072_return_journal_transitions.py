"""Prove one complete return transition against its fixed receipt and immutable journals."""

from alembic import op

revision = "20261109_0072"
down_revision = "20261109_0071"
branch_labels = None
depends_on = None


_TRANSITION = r"""
CREATE FUNCTION coinpup_validate_return_transition(revision_identifier uuid) RETURNS void
LANGUAGE plpgsql SECURITY INVOKER SET search_path=pg_catalog AS $$
DECLARE h public.business_return_revisions%ROWTYPE; m record;
BEGIN
    SELECT * INTO h FROM public.business_return_revisions WHERE id=revision_identifier;
    -- The fixed 200 already proves the unique immediately preceding group, complete member
    -- coverage, the retained operation identities and the order of removed and new members.
    PERFORM public.coinpup_validate_return_revision_receipt(revision_identifier);
    FOR m IN SELECT member.*,r.reverses_journal_id,r.revision_reason,
                    r.operation_version AS reverse_version,r.operation_id AS reverse_operation,
                    r.ledger_id AS reverse_ledger
        FROM public.business_return_revision_members member
        LEFT JOIN public.journals r ON r.id=member.reversal_journal_id
        WHERE member.revision_id=h.id ORDER BY member.member_no
    LOOP
        IF m.action<>'create' THEN
            IF ROW(m.reverse_ledger,m.reverse_operation,m.reverse_version,m.reverses_journal_id,
                   m.revision_reason) IS DISTINCT FROM
               ROW(h.ledger_id,m.operation_id,m.operation_version,m.previous_journal_id,
                   h.revision_reason) THEN
                RAISE EXCEPTION USING ERRCODE='23514',
                    MESSAGE='Allocation return reversal does not match its transition',
                    CONSTRAINT='ck_return_revision_shape';
            END IF;
            -- The generic reversal proof checks the sealed predecessor, original dates and
            -- description, components, accounts, per-asset amounts and complete inverse lines.
            -- The deferred journal dimension trigger additionally checks the six dimensions.
            PERFORM public.coinpup_validate_reversal(m.reversal_journal_id);
        END IF;
        IF m.action<>'cancel' THEN
            PERFORM public.coinpup_validate_return_posting_fact(m.journal_id,
                public.coinpup_validate_journal_common(m.journal_id));
        END IF;
        PERFORM public.coinpup_validate_return_operation_owner(m.operation_id);
    END LOOP;
END; $$;
"""


def upgrade():
    op.execute("SELECT pg_advisory_xact_lock(18945999704708432)")
    op.execute(_TRANSITION)


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
    op.execute("DROP FUNCTION coinpup_validate_return_transition(uuid)")
