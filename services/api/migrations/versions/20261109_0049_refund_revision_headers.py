"""Add immutable refund revision headers without opening any financial revision writes."""

from alembic import op

revision = "20261109_0049"
down_revision = "20261106_0048"
branch_labels = None
depends_on = None

_TABLES = ("business_refund_revisions",)
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
)
_ENTITY_TYPES = ", ".join(repr(value) for value in _OLD_ENTITY_TYPES + _TABLES)
_CHANGE_TRIGGER_SQL = {
    table: f"CREATE TRIGGER trg_{table}_change_log AFTER INSERT OR UPDATE ON {table} "
    "FOR EACH ROW EXECUTE FUNCTION coinpup_record_change()"
    for table in _TABLES
}

_SCHEMA_SQL = """
CREATE TABLE business_refund_revisions (
    refund_id UUID NOT NULL,
    payment_id UUID NOT NULL,
    operation_id UUID NOT NULL,
    operation_version INTEGER NOT NULL,
    previous_revision_id UUID,
    action VARCHAR(7) NOT NULL,
    revision_reason VARCHAR(1000) NOT NULL,
    previous_journal_id UUID NOT NULL,
    reversal_journal_id UUID NOT NULL,
    replacement_journal_id UUID,
    account_id UUID,
    asset_id VARCHAR(200),
    amount NUMERIC(38, 18),
    payment_amount NUMERIC(38, 18),
    transaction_date DATE,
    reason VARCHAR(2000),
    fees JSONB,
    created_by UUID NOT NULL,
    idempotency_key VARCHAR(128) NOT NULL,
    id UUID NOT NULL,
    ledger_id UUID NOT NULL,
    version INTEGER DEFAULT '1' NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    PRIMARY KEY (id),
    CONSTRAINT uq_refund_revision_id_ledger UNIQUE (id, ledger_id),
    CONSTRAINT fk_refund_revision_ledger FOREIGN KEY(ledger_id) REFERENCES ledgers (id) ON DELETE
        RESTRICT,
    CONSTRAINT ck_refund_revision_version CHECK (version = 1),
    CONSTRAINT uq_refund_revision_key UNIQUE (ledger_id, idempotency_key),
    CONSTRAINT fk_refund_revision_owner FOREIGN KEY(ledger_id, created_by) REFERENCES ledgers (id,
        owner_id) ON DELETE RESTRICT,
    CONSTRAINT fk_refund_revision_receipt FOREIGN KEY(ledger_id, idempotency_key) REFERENCES
        command_receipts (ledger_id, key) DEFERRABLE INITIALLY DEFERRED,
    CONSTRAINT uq_refund_revision_scope UNIQUE (id, refund_id, payment_id, ledger_id),
    CONSTRAINT uq_refund_revision_version UNIQUE (refund_id, operation_version),
    CONSTRAINT uq_refund_revision_reversal UNIQUE (reversal_journal_id),
    CONSTRAINT uq_refund_revision_replacement UNIQUE (replacement_journal_id),
    CONSTRAINT fk_refund_revision_original FOREIGN KEY(refund_id, payment_id, operation_id,
        ledger_id) REFERENCES business_refunds (id, payment_id, operation_id, ledger_id) ON DELETE
        RESTRICT,
    CONSTRAINT fk_refund_revision_predecessor FOREIGN KEY(previous_revision_id, refund_id,
        payment_id, ledger_id) REFERENCES business_refund_revisions (id, refund_id, payment_id,
        ledger_id) ON DELETE RESTRICT,
    CONSTRAINT fk_refund_revision_account_asset FOREIGN KEY(account_id, asset_id, ledger_id)
        REFERENCES account_assets (account_id, asset_id, ledger_id) ON DELETE RESTRICT,
    CONSTRAINT fk_refund_revision_previous_journal FOREIGN KEY(previous_journal_id, operation_id,
        ledger_id) REFERENCES journals (id, operation_id, ledger_id) DEFERRABLE INITIALLY DEFERRED,
    CONSTRAINT fk_refund_revision_reversal_journal FOREIGN KEY(reversal_journal_id, operation_id,
        ledger_id) REFERENCES journals (id, operation_id, ledger_id) DEFERRABLE INITIALLY DEFERRED,
    CONSTRAINT fk_refund_revision_replacement_journal FOREIGN KEY(replacement_journal_id,
        operation_id, ledger_id) REFERENCES journals (id, operation_id, ledger_id) DEFERRABLE
        INITIALLY DEFERRED,
    CONSTRAINT ck_refund_revision_predecessor CHECK ((operation_version = 2 AND previous_revision_id
        IS NULL) OR (operation_version > 2 AND previous_revision_id IS NOT NULL AND
        previous_revision_id <> id)),
    CONSTRAINT ck_refund_revision_journals CHECK (previous_journal_id <> reversal_journal_id AND
        (replacement_journal_id IS NULL OR (replacement_journal_id <> previous_journal_id AND
        replacement_journal_id <> reversal_journal_id))),
    CONSTRAINT ck_refund_revision_reason CHECK (revision_reason ~ '^[^[:space:]]' AND
        revision_reason ~ '[^[:space:]]$'),
    CONSTRAINT ck_refund_revision_replacement CHECK ((action = 'correct' AND
        num_nonnulls(replacement_journal_id, account_id, asset_id, amount, payment_amount,
        transaction_date, reason, fees) = 8) OR (action = 'cancel' AND
        num_nonnulls(replacement_journal_id, account_id, asset_id, amount, payment_amount,
        transaction_date, reason, fees) = 0)),
    CONSTRAINT ck_refund_revision_amounts CHECK (amount > 0 AND amount < 100000000000000000000 AND
        payment_amount > 0 AND payment_amount < 100000000000000000000),
    CONSTRAINT ck_refund_revision_date CHECK (transaction_date BETWEEN DATE '0001-01-01' AND DATE
        '9999-12-31'),
    CONSTRAINT ck_refund_revision_fact_reason CHECK (reason ~ '^[^[:space:]]' AND reason ~
        '[^[:space:]]$'),
    CONSTRAINT ck_refund_revision_fees CHECK (CASE WHEN fees IS NULL THEN true WHEN
        jsonb_typeof(fees) = 'array' THEN jsonb_array_length(fees) <= 20 ELSE false END)
);
"""

_GUARDS = r"""
CREATE FUNCTION coinpup_guard_refund_revision() RETURNS trigger
LANGUAGE plpgsql SET search_path=pg_catalog AS $$
BEGIN
    IF TG_OP <> 'INSERT' THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='Refund revision history is immutable',
            CONSTRAINT='ck_refund_revision_immutable';
    END IF;
    PERFORM pg_advisory_xact_lock(18945999704708432);
    IF current_setting('transaction_isolation') <> 'read committed' THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='Refund revision isolation is invalid',
            CONSTRAINT='ck_refund_revision_isolation';
    END IF;
    RETURN NEW;
END; $$;

CREATE FUNCTION coinpup_validate_refund_revision(revision_identifier uuid) RETURNS void
LANGUAGE plpgsql SET search_path=pg_catalog AS $$
BEGIN
    RAISE EXCEPTION USING ERRCODE='23514',
        MESSAGE='Refund revision requires complete refund validation',
        CONSTRAINT='ck_refund_revision_shape';
END; $$;

CREATE FUNCTION coinpup_check_refund_revision() RETURNS trigger
LANGUAGE plpgsql SET search_path=pg_catalog AS $$
BEGIN
    PERFORM public.coinpup_validate_refund_revision(NEW.id);
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
        "uq_business_refund_operation_scope",
        "business_refunds",
        ["id", "payment_id", "operation_id", "ledger_id"],
    )
    op.execute(_SCHEMA_SQL)
    op.execute(_GUARDS)
    _change_types(_OLD_ENTITY_TYPES + _TABLES)
    op.execute(
        "CREATE TRIGGER tr_business_refund_revisions_guard BEFORE INSERT OR UPDATE OR DELETE "
        "ON business_refund_revisions FOR EACH ROW "
        "EXECUTE FUNCTION coinpup_guard_refund_revision()"
    )
    op.execute(
        "CREATE CONSTRAINT TRIGGER tr_business_refund_revisions_complete AFTER INSERT "
        "ON business_refund_revisions DEFERRABLE INITIALLY DEFERRED FOR EACH ROW "
        "EXECUTE FUNCTION coinpup_check_refund_revision()"
    )
    op.execute(_CHANGE_TRIGGER_SQL["business_refund_revisions"])


def downgrade():
    op.execute("SELECT pg_advisory_xact_lock(18945999704708432)")
    op.execute("LOCK TABLE public.business_refund_revisions IN ACCESS EXCLUSIVE MODE")
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM public.business_refund_revisions)
               OR EXISTS (SELECT 1 FROM public.change_log
                   WHERE entity_type='business_refund_revisions'
                      OR (entity_type='business_refunds' AND entity_version>1)) THEN
                RAISE EXCEPTION USING ERRCODE='23514',
                    MESSAGE='Refund revision history prevents downgrade',
                    CONSTRAINT='ck_refund_revision_downgrade';
            END IF;
        END; $$;
    """)
    op.drop_table("business_refund_revisions")
    op.drop_constraint("uq_business_refund_operation_scope", "business_refunds", type_="unique")
    for function in (
        "coinpup_check_refund_revision()",
        "coinpup_validate_refund_revision(uuid)",
        "coinpup_guard_refund_revision()",
    ):
        op.execute("DROP FUNCTION " + function)
    _change_types(_OLD_ENTITY_TYPES)
