"""Bind immutable return member transitions to their source, prefix and posting history."""

from alembic import op

revision = "20261109_0061"
down_revision = "20261109_0060"
branch_labels = None
depends_on = None

_TABLES = ("business_return_revision_members",)
_OLD_ENTITY_TYPES = (
    "entities",
    "ledgers",
    "accounts",
    "account_assets",
    "categories",
    "assets",
    "financial_operations",
    "stored_files",
    "operation_file_links",
    "ocr_jobs",
    "ocr_drafts",
    "ocr_confirmations",
    "business_parties",
    "business_documents",
    "business_document_lines",
    "ledger_periods",
    "ledger_period_audits",
    "ledger_period_receipts",
    "business_projects",
    "recurring_invoice_rules",
    "recurring_invoice_instances",
    "reminder_events",
    "reminder_event_revisions",
    "reminder_delivery_configs",
    "reminder_delivery_revisions",
    "business_confirmations",
    "business_postings",
    "business_payments",
    "business_allocations",
    "business_allocation_lines",
    "business_allocation_returns",
    "business_allocation_return_lines",
    "business_refunds",
    "business_refund_returns",
    "business_credits",
    "business_credit_lines",
    "business_credit_revisions",
    "business_credit_revision_members",
    "business_refund_revisions",
    "business_refund_revision_returns",
    "business_return_revisions",
)
_ENTITY_TYPES = ", ".join(repr(value) for value in _OLD_ENTITY_TYPES + _TABLES)
_CHANGE_TRIGGER_SQL = {
    table: f"CREATE TRIGGER trg_{table}_change_log AFTER INSERT OR UPDATE ON {table} "
    "FOR EACH ROW EXECUTE FUNCTION coinpup_record_change()"
    for table in _TABLES
}

_SCHEMA_SQL = r"""
CREATE TABLE business_return_revision_members (
    revision_id UUID NOT NULL,
    return_id UUID NOT NULL,
    payment_id UUID NOT NULL,
    source_allocation_line_id UUID NOT NULL,
    source_operation_id UUID NOT NULL,
    member_no INTEGER NOT NULL,
    action VARCHAR(7) NOT NULL,
    operation_id UUID NOT NULL,
    operation_version INTEGER NOT NULL,
    previous_line_no INTEGER,
    previous_operation_version INTEGER,
    previous_journal_id UUID,
    reversal_journal_id UUID,
    line_no INTEGER,
    journal_id UUID,
    source_version INTEGER NOT NULL,
    source_return_no BIGINT NOT NULL,
    source_predecessor_operation_id UUID,
    prefix_payment_amount NUMERIC(38, 18) NOT NULL,
    prefix_document_amount NUMERIC(38, 18) NOT NULL,
    payment_amount NUMERIC(38, 18),
    document_amount NUMERIC(38, 18),
    absorbs_remainder BOOLEAN,
    id UUID NOT NULL,
    ledger_id UUID NOT NULL,
    version INTEGER DEFAULT '1' NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    PRIMARY KEY (id),
    CONSTRAINT uq_return_revision_member_id_ledger UNIQUE (id, ledger_id),
    CONSTRAINT fk_return_revision_member_ledger FOREIGN KEY(ledger_id) REFERENCES ledgers (id) ON
        DELETE RESTRICT,
    CONSTRAINT ck_return_revision_member_version CHECK (version = 1),
    CONSTRAINT uq_return_revision_member_position UNIQUE (revision_id, member_no),
    CONSTRAINT uq_return_revision_member_source UNIQUE (revision_id, source_allocation_line_id),
    CONSTRAINT uq_return_revision_member_operation UNIQUE (revision_id, operation_id),
    CONSTRAINT uq_return_revision_member_previous_position UNIQUE (revision_id, previous_line_no),
    CONSTRAINT uq_return_revision_member_next_position UNIQUE (revision_id, line_no),
    CONSTRAINT uq_return_revision_member_version UNIQUE (operation_id, operation_version),
    CONSTRAINT uq_return_revision_member_journal UNIQUE (journal_id),
    CONSTRAINT uq_return_revision_member_reversal UNIQUE (reversal_journal_id),
    CONSTRAINT fk_return_revision_member_group FOREIGN KEY(revision_id, return_id, payment_id,
        ledger_id) REFERENCES business_return_revisions (id, return_id, payment_id, ledger_id) ON
        DELETE RESTRICT,
    CONSTRAINT fk_return_revision_member_source FOREIGN KEY(source_allocation_line_id, payment_id,
        source_operation_id, ledger_id) REFERENCES business_allocation_lines (id, payment_id,
        operation_id, ledger_id) ON DELETE RESTRICT,
    CONSTRAINT fk_return_revision_member_operation FOREIGN KEY(operation_id, ledger_id) REFERENCES
        financial_operations (id, ledger_id) ON DELETE RESTRICT,
    CONSTRAINT fk_return_revision_member_source_predecessor FOREIGN
        KEY(source_predecessor_operation_id, ledger_id) REFERENCES financial_operations (id,
        ledger_id) ON DELETE RESTRICT,
    CONSTRAINT fk_return_revision_member_previous FOREIGN KEY(previous_journal_id, operation_id,
        ledger_id) REFERENCES journals (id, operation_id, ledger_id) DEFERRABLE INITIALLY
        DEFERRED,
    CONSTRAINT fk_return_revision_member_reversal FOREIGN KEY(reversal_journal_id, operation_id,
        ledger_id) REFERENCES journals (id, operation_id, ledger_id) DEFERRABLE INITIALLY
        DEFERRED,
    CONSTRAINT fk_return_revision_member_journal FOREIGN KEY(journal_id, operation_id, ledger_id)
        REFERENCES journals (id, operation_id, ledger_id) DEFERRABLE INITIALLY DEFERRED,
    CONSTRAINT ck_return_revision_member_source_order CHECK (source_version = 1 AND
        source_return_no >= 1),
    CONSTRAINT ck_return_revision_member_source_predecessor CHECK ((source_return_no = 1 AND
        source_predecessor_operation_id IS NULL) OR (source_return_no > 1 AND
        source_predecessor_operation_id IS NOT NULL AND source_predecessor_operation_id <>
        operation_id AND source_predecessor_operation_id <> source_operation_id)),
    CONSTRAINT ck_return_revision_member_distinct_operation CHECK (source_operation_id <>
        operation_id),
    CONSTRAINT ck_return_revision_member_prefix CHECK (prefix_payment_amount >= 0 AND
        prefix_payment_amount < 100000000000000000000 AND prefix_document_amount >= 0 AND
        prefix_document_amount < 100000000000000000000 AND (prefix_payment_amount = 0) =
        (prefix_document_amount = 0) AND (source_return_no > 1 OR prefix_payment_amount = 0)),
    CONSTRAINT ck_return_revision_member_position CHECK (member_no BETWEEN 1 AND 400),
    CONSTRAINT ck_return_revision_member_action CHECK (action IN ('create', 'correct', 'cancel')),
    CONSTRAINT ck_return_revision_member_previous CHECK ((action = 'create' AND previous_line_no
        IS NULL AND previous_operation_version IS NULL AND previous_journal_id IS NULL AND
        reversal_journal_id IS NULL AND operation_version = 1) OR (action IN ('correct', 'cancel')
        AND previous_line_no IS NOT NULL AND previous_line_no BETWEEN 1 AND 200 AND
        previous_operation_version IS NOT NULL AND previous_operation_version BETWEEN 1 AND
        2147483646 AND operation_version::bigint = previous_operation_version::bigint + 1 AND
        previous_journal_id IS NOT NULL AND reversal_journal_id IS NOT NULL AND
        reversal_journal_id <> previous_journal_id)),
    CONSTRAINT ck_return_revision_member_replacement CHECK ((action IN ('create', 'correct') AND
        line_no IS NOT NULL AND line_no BETWEEN 1 AND 200 AND journal_id IS NOT NULL AND
        payment_amount IS NOT NULL AND document_amount IS NOT NULL AND payment_amount > 0 AND
        payment_amount < 100000000000000000000 AND document_amount > 0 AND document_amount <
        100000000000000000000 AND absorbs_remainder IS NOT NULL AND (action = 'create' OR
        (journal_id <> previous_journal_id AND journal_id <> reversal_journal_id))) OR (action =
        'cancel' AND line_no IS NULL AND journal_id IS NULL AND payment_amount IS NULL AND
        document_amount IS NULL AND absorbs_remainder IS NULL))
);
CREATE INDEX ix_return_revision_member_source_order ON business_return_revision_members
        (ledger_id, source_allocation_line_id, source_return_no);
"""

_GUARDS = r"""
CREATE FUNCTION coinpup_check_return_revision_member() RETURNS trigger
LANGUAGE plpgsql SET search_path=pg_catalog AS $$
BEGIN
    PERFORM public.coinpup_validate_return_revision(NEW.revision_id);
    RETURN NULL;
END; $$;
"""


def _change_types(types):
    op.drop_constraint("ck_change_log_entity_type", "change_log", type_="check")
    op.create_check_constraint(
        "ck_change_log_entity_type",
        "change_log",
        "entity_type IN (" + ", ".join(repr(value) for value in types) + ")",
    )


def upgrade():
    op.execute("SELECT pg_advisory_xact_lock(18945999704708432)")
    op.create_unique_constraint(
        "uq_allocation_line_payment_operation_scope",
        "business_allocation_lines",
        ["id", "payment_id", "operation_id", "ledger_id"],
    )
    op.execute(_SCHEMA_SQL)
    op.execute(_GUARDS)
    _change_types(_OLD_ENTITY_TYPES + _TABLES)
    op.execute(
        "CREATE TRIGGER tr_return_revision_members_guard BEFORE INSERT OR UPDATE OR DELETE "
        "ON business_return_revision_members FOR EACH ROW "
        "EXECUTE FUNCTION coinpup_guard_return_revision()"
    )
    op.execute(
        "CREATE CONSTRAINT TRIGGER tr_return_revision_members_complete AFTER INSERT "
        "ON business_return_revision_members DEFERRABLE INITIALLY DEFERRED FOR EACH ROW "
        "EXECUTE FUNCTION coinpup_check_return_revision_member()"
    )
    op.execute(_CHANGE_TRIGGER_SQL["business_return_revision_members"])


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
    op.drop_table("business_return_revision_members")
    op.drop_constraint(
        "uq_allocation_line_payment_operation_scope", "business_allocation_lines", type_="unique"
    )
    op.execute("DROP FUNCTION coinpup_check_return_revision_member()")
    _change_types(_OLD_ENTITY_TYPES)
