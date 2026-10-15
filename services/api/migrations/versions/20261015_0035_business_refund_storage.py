"""Immutable actual-refund storage, gated until dedicated posting validation is installed."""

from alembic import op

revision = "20261015_0035"
down_revision = "20261014_0034"
branch_labels = None
depends_on = None

_TABLES = ("business_refunds", "business_refund_returns")
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
)
_ENTITY_TYPES = ", ".join(repr(value) for value in _OLD_ENTITY_TYPES + _TABLES)
_CHANGE_TRIGGER_SQL = {
    table: f"CREATE TRIGGER trg_{table}_change_log AFTER INSERT OR UPDATE ON {table} "
    "FOR EACH ROW EXECUTE FUNCTION coinpup_record_change()"
    for table in _TABLES
}

_SCHEMA_SQL = """
CREATE TABLE business_refunds (
    payment_id UUID NOT NULL,
    source_version INTEGER NOT NULL,
    account_id UUID NOT NULL,
    asset_id VARCHAR(200) NOT NULL,
    amount NUMERIC(38, 18) NOT NULL,
    payment_amount NUMERIC(38, 18) NOT NULL,
    transaction_date DATE NOT NULL,
    reason VARCHAR(2000) NOT NULL,
    fees JSONB DEFAULT '[]'::jsonb NOT NULL,
    created_by UUID NOT NULL,
    idempotency_key VARCHAR(128) NOT NULL,
    operation_id UUID NOT NULL,
    journal_id UUID NOT NULL,
    id UUID NOT NULL,
    ledger_id UUID NOT NULL,
    version INTEGER DEFAULT '1' NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    PRIMARY KEY (id),
    CONSTRAINT uq_business_refund_id_ledger UNIQUE (id, ledger_id),
    CONSTRAINT fk_business_refund_ledger FOREIGN KEY(ledger_id) REFERENCES ledgers (id) ON
    DELETE RESTRICT,
    CONSTRAINT ck_business_refund_version CHECK (version = 1),
    CONSTRAINT uq_business_refund_operation UNIQUE (operation_id),
    CONSTRAINT uq_business_refund_journal UNIQUE (journal_id),
    CONSTRAINT fk_business_refund_journal FOREIGN KEY(journal_id, operation_id, ledger_id)
    REFERENCES journals (id, operation_id, ledger_id) DEFERRABLE INITIALLY DEFERRED,
    CONSTRAINT uq_business_refund_key UNIQUE (ledger_id, idempotency_key),
    CONSTRAINT fk_business_refund_owner FOREIGN KEY(ledger_id, created_by) REFERENCES ledgers
    (id, owner_id) ON DELETE RESTRICT,
    CONSTRAINT fk_business_refund_receipt FOREIGN KEY(ledger_id, idempotency_key) REFERENCES
    command_receipts (ledger_id, key) DEFERRABLE INITIALLY DEFERRED,
    CONSTRAINT uq_business_refund_scope UNIQUE (id, payment_id, ledger_id),
    CONSTRAINT fk_business_refund_payment FOREIGN KEY(payment_id, ledger_id) REFERENCES
    business_payments (id, ledger_id) ON DELETE RESTRICT,
    CONSTRAINT fk_business_refund_account_asset FOREIGN KEY(account_id, asset_id, ledger_id)
    REFERENCES account_assets (account_id, asset_id, ledger_id) ON DELETE RESTRICT,
    CONSTRAINT ck_business_refund_source_version CHECK (source_version >= 1),
    CONSTRAINT ck_business_refund_amounts CHECK (amount > 0 AND amount < 100000000000000000000
    AND payment_amount > 0 AND payment_amount < 100000000000000000000),
    CONSTRAINT ck_business_refund_date CHECK (transaction_date BETWEEN DATE '0001-01-01' AND
    DATE '9999-12-31'),
    CONSTRAINT ck_business_refund_reason CHECK (btrim(reason) <> ''),
    CONSTRAINT ck_business_refund_fees CHECK (CASE WHEN jsonb_typeof(fees) = 'array' THEN
    jsonb_array_length(fees) <= 20 ELSE false END)
);

CREATE INDEX ix_business_refunds_ledger_created ON business_refunds (ledger_id, created_at, id);

CREATE INDEX ix_business_refunds_payment ON business_refunds (payment_id, created_at, id);

CREATE TABLE business_refund_returns (
    id UUID DEFAULT gen_random_uuid() NOT NULL,
    refund_id UUID NOT NULL,
    payment_id UUID NOT NULL,
    return_id UUID NOT NULL,
    line_no INTEGER NOT NULL,
    ledger_id UUID NOT NULL,
    version INTEGER DEFAULT '1' NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    PRIMARY KEY (id),
    CONSTRAINT uq_business_refund_return_id_ledger UNIQUE (id, ledger_id),
    CONSTRAINT fk_business_refund_return_ledger FOREIGN KEY(ledger_id) REFERENCES ledgers (id)
    ON DELETE RESTRICT,
    CONSTRAINT ck_business_refund_return_version CHECK (version = 1),
    CONSTRAINT uq_business_refund_return_position UNIQUE (refund_id, line_no),
    CONSTRAINT uq_business_refund_return_source UNIQUE (refund_id, return_id),
    CONSTRAINT fk_business_refund_return_group FOREIGN KEY(refund_id, payment_id, ledger_id)
    REFERENCES business_refunds (id, payment_id, ledger_id) ON DELETE RESTRICT,
    CONSTRAINT fk_business_refund_return_source FOREIGN KEY(return_id, payment_id, ledger_id)
    REFERENCES business_allocation_returns (id, payment_id, ledger_id) ON DELETE RESTRICT,
    CONSTRAINT ck_business_refund_return_position CHECK (line_no BETWEEN 1 AND 200)
);

CREATE INDEX ix_business_refund_returns_source ON business_refund_returns (return_id);

"""


_GUARDS = r"""
CREATE FUNCTION coinpup_guard_business_refund() RETURNS trigger
LANGUAGE plpgsql SET search_path=pg_catalog AS $$
BEGIN
    IF TG_OP <> 'INSERT' THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='Business refund history is immutable',
            CONSTRAINT='ck_business_refund_immutable';
    END IF;
    PERFORM pg_advisory_xact_lock(18945999704708432);
    IF current_setting('transaction_isolation') <> 'read committed' THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='Business refund isolation is invalid',
            CONSTRAINT='ck_business_refund_isolation';
    END IF;
    RETURN NEW;
END; $$;

CREATE FUNCTION coinpup_validate_business_refund(refund_identifier uuid) RETURNS void
LANGUAGE plpgsql SET search_path=pg_catalog AS $$
BEGIN
    RAISE EXCEPTION USING ERRCODE='23514',
        MESSAGE='Business refund requires dedicated validation',
        CONSTRAINT='ck_business_refund_shape';
END; $$;

CREATE FUNCTION coinpup_check_business_refund() RETURNS trigger
LANGUAGE plpgsql SET search_path=pg_catalog AS $$
BEGIN
    IF TG_TABLE_NAME='business_refunds' THEN
        PERFORM public.coinpup_validate_business_refund(NEW.id);
    ELSE
        PERFORM public.coinpup_validate_business_refund(NEW.refund_id);
    END IF;
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
    for table in _TABLES:
        op.execute(
            f"CREATE TRIGGER tr_{table}_guard BEFORE INSERT OR UPDATE OR DELETE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION coinpup_guard_business_refund()"
        )
        op.execute(
            f"CREATE CONSTRAINT TRIGGER tr_{table}_complete AFTER INSERT ON {table} "
            "DEFERRABLE INITIALLY DEFERRED FOR EACH ROW "
            "EXECUTE FUNCTION coinpup_check_business_refund()"
        )
        op.execute(_CHANGE_TRIGGER_SQL[table])


def downgrade():
    op.execute("SELECT pg_advisory_xact_lock(18945999704708432)")
    op.execute("LOCK TABLE " + ", ".join(_TABLES) + " IN ACCESS EXCLUSIVE MODE")
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM public.business_refunds)
               OR EXISTS (SELECT 1 FROM public.business_refund_returns)
               OR EXISTS (SELECT 1 FROM public.change_log WHERE entity_type IN
                   ('business_refunds','business_refund_returns')) THEN
                RAISE EXCEPTION USING ERRCODE='23514',
                    MESSAGE='Business refund history prevents downgrade',
                    CONSTRAINT='ck_business_refund_downgrade';
            END IF;
        END; $$;
    """)
    for table in reversed(_TABLES):
        op.drop_table(table)
    for function in (
        "coinpup_check_business_refund()",
        "coinpup_validate_business_refund(uuid)",
        "coinpup_guard_business_refund()",
    ):
        op.execute("DROP FUNCTION " + function)
    _change_types(_OLD_ENTITY_TYPES)
