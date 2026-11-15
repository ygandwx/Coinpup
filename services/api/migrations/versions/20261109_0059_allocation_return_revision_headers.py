"""Add immutable allocation return revision headers with a closed commit gate."""

from alembic import op

revision = "20261109_0059"
down_revision = "20261109_0058"
branch_labels = None
depends_on = None

_TABLES = ("business_return_revisions",)
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
)
_ENTITY_TYPES = ", ".join(repr(value) for value in _OLD_ENTITY_TYPES + _TABLES)
_CHANGE_TRIGGER_SQL = {
    table: f"CREATE TRIGGER trg_{table}_change_log AFTER INSERT OR UPDATE ON {table} "
    "FOR EACH ROW EXECUTE FUNCTION coinpup_record_change()"
    for table in _TABLES
}

_SCHEMA_SQL = """
CREATE TABLE business_return_revisions (
    return_id UUID NOT NULL,
    payment_id UUID NOT NULL,
    group_version INTEGER NOT NULL,
    previous_revision_id UUID,
    action VARCHAR(7) NOT NULL,
    revision_reason VARCHAR(1000) NOT NULL,
    return_date DATE,
    return_reason VARCHAR(2000),
    created_by UUID NOT NULL,
    idempotency_key VARCHAR(128) NOT NULL,
    id UUID NOT NULL,
    ledger_id UUID NOT NULL,
    version INTEGER DEFAULT '1' NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    PRIMARY KEY (id),
    CONSTRAINT uq_business_return_revision_id_ledger UNIQUE (id, ledger_id),
    CONSTRAINT fk_business_return_revision_ledger FOREIGN KEY(ledger_id) REFERENCES ledgers (id)
    ON DELETE RESTRICT,
    CONSTRAINT ck_business_return_revision_version CHECK (version = 1),
    CONSTRAINT uq_business_return_revision_key UNIQUE (ledger_id, idempotency_key),
    CONSTRAINT fk_business_return_revision_owner FOREIGN KEY(ledger_id, created_by) REFERENCES
    ledgers (id, owner_id) ON DELETE RESTRICT,
    CONSTRAINT fk_business_return_revision_receipt FOREIGN KEY(ledger_id, idempotency_key)
    REFERENCES command_receipts (ledger_id, key) DEFERRABLE INITIALLY DEFERRED,
    CONSTRAINT uq_business_return_revision_scope UNIQUE (id, return_id, payment_id, ledger_id),
    CONSTRAINT uq_business_return_revision_group_version UNIQUE (return_id, group_version),
    CONSTRAINT fk_business_return_revision_group FOREIGN KEY(return_id, payment_id, ledger_id)
    REFERENCES business_allocation_returns (id, payment_id, ledger_id) ON DELETE RESTRICT,
    CONSTRAINT fk_business_return_revision_previous FOREIGN KEY(previous_revision_id, return_id,
    payment_id, ledger_id) REFERENCES business_return_revisions (id, return_id, payment_id,
    ledger_id) ON DELETE RESTRICT,
    CONSTRAINT ck_business_return_revision_group_version CHECK (group_version >= 2),
    CONSTRAINT ck_business_return_revision_previous CHECK ((group_version = 2 AND
    previous_revision_id IS NULL) OR (group_version > 2 AND previous_revision_id IS NOT NULL AND
    previous_revision_id <> id)),
    CONSTRAINT ck_business_return_revision_action CHECK (action IN ('correct', 'cancel')),
    CONSTRAINT ck_business_return_revision_reason CHECK (revision_reason ~
    '^[^[:space:]]' AND revision_reason ~ '[^[:space:]]$'),
    CONSTRAINT ck_business_return_revision_replacement CHECK ((action = 'correct' AND
    return_date IS NOT NULL AND return_reason IS NOT NULL AND return_date BETWEEN DATE
    '0001-01-01' AND DATE '9999-12-31' AND return_reason ~ '^[^[:space:]]' AND
    return_reason ~ '[^[:space:]]$') OR (action = 'cancel' AND
    return_date IS NULL AND return_reason IS NULL))
);
"""

_GUARDS = r"""
CREATE FUNCTION coinpup_guard_return_revision() RETURNS trigger
LANGUAGE plpgsql SET search_path=pg_catalog AS $$
BEGIN
    IF TG_OP <> 'INSERT' THEN
        RAISE EXCEPTION USING ERRCODE='23514',
            MESSAGE='Allocation return revision history is immutable',
            CONSTRAINT='ck_return_revision_immutable';
    END IF;
    PERFORM pg_advisory_xact_lock(18945999704708432);
    IF current_setting('transaction_isolation') <> 'read committed' THEN
        RAISE EXCEPTION USING ERRCODE='23514',
            MESSAGE='Allocation return revision isolation is invalid',
            CONSTRAINT='ck_return_revision_isolation';
    END IF;
    RETURN NEW;
END; $$;

CREATE FUNCTION coinpup_validate_return_revision(revision_identifier uuid) RETURNS void
LANGUAGE plpgsql SET search_path=pg_catalog AS $$
BEGIN
    RAISE EXCEPTION USING ERRCODE='23514',
        MESSAGE='Allocation return revision requires complete group validation',
        CONSTRAINT='ck_return_revision_shape';
END; $$;

CREATE FUNCTION coinpup_check_return_revision() RETURNS trigger
LANGUAGE plpgsql SET search_path=pg_catalog AS $$
BEGIN
    PERFORM public.coinpup_validate_return_revision(NEW.id);
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
    op.execute(_SCHEMA_SQL)
    op.execute(_GUARDS)
    _change_types(_OLD_ENTITY_TYPES + _TABLES)
    op.execute(
        "CREATE TRIGGER tr_business_return_revisions_guard BEFORE INSERT OR UPDATE OR DELETE "
        "ON business_return_revisions FOR EACH ROW "
        "EXECUTE FUNCTION coinpup_guard_return_revision()"
    )
    op.execute(
        "CREATE CONSTRAINT TRIGGER tr_business_return_revisions_complete AFTER INSERT "
        "ON business_return_revisions DEFERRABLE INITIALLY DEFERRED FOR EACH ROW "
        "EXECUTE FUNCTION coinpup_check_return_revision()"
    )
    op.execute(_CHANGE_TRIGGER_SQL["business_return_revisions"])


def downgrade():
    op.execute("SELECT pg_advisory_xact_lock(18945999704708432)")
    op.execute("LOCK TABLE public.business_return_revisions IN ACCESS EXCLUSIVE MODE")
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM public.business_return_revisions)
               OR EXISTS (SELECT 1 FROM public.change_log
                   WHERE entity_type='business_return_revisions'
                      OR (entity_type='business_allocation_returns' AND entity_version>1)) THEN
                RAISE EXCEPTION USING ERRCODE='23514',
                    MESSAGE='Allocation return revision history prevents downgrade',
                    CONSTRAINT='ck_return_revision_downgrade';
            END IF;
        END; $$;
    """)
    op.drop_table("business_return_revisions")
    for function in (
        "coinpup_check_return_revision()",
        "coinpup_validate_return_revision(uuid)",
        "coinpup_guard_return_revision()",
    ):
        op.execute("DROP FUNCTION " + function)
    _change_types(_OLD_ENTITY_TYPES)
