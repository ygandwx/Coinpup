"""Immutable manual-credit storage, gated until dedicated posting validation is installed."""

from alembic import op

revision = "20261021_0037"
down_revision = "20261015_0036"
branch_labels = None
depends_on = None

_TABLES = ("business_credits", "business_credit_lines")
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
)

_ENTITY_TYPES = ", ".join(repr(value) for value in _OLD_ENTITY_TYPES + _TABLES)
_CHANGE_TRIGGER_SQL = {
    table: f"CREATE TRIGGER trg_{table}_change_log AFTER INSERT OR UPDATE ON {table} "
    "FOR EACH ROW EXECUTE FUNCTION coinpup_record_change()"
    for table in _TABLES
}

_SCHEMA_SQL = """
CREATE TABLE business_credits (
    document_id UUID NOT NULL,
    source_version INTEGER NOT NULL,
    credit_date DATE NOT NULL,
    reason VARCHAR(2000) NOT NULL,
    created_by UUID NOT NULL,
    idempotency_key VARCHAR(128) NOT NULL,
    id UUID NOT NULL,
    ledger_id UUID NOT NULL,
    version INTEGER DEFAULT '1' NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    PRIMARY KEY (id),
    CONSTRAINT uq_business_credit_id_ledger UNIQUE (id, ledger_id),
    CONSTRAINT fk_business_credit_ledger FOREIGN KEY(ledger_id) REFERENCES ledgers (id) ON
    DELETE RESTRICT,
    CONSTRAINT ck_business_credit_version CHECK (version = 1),
    CONSTRAINT uq_business_credit_key UNIQUE (ledger_id, idempotency_key),
    CONSTRAINT fk_business_credit_owner FOREIGN KEY(ledger_id, created_by) REFERENCES ledgers
    (id, owner_id) ON DELETE RESTRICT,
    CONSTRAINT fk_business_credit_receipt FOREIGN KEY(ledger_id, idempotency_key) REFERENCES
    command_receipts (ledger_id, key) DEFERRABLE INITIALLY DEFERRED,
    CONSTRAINT uq_business_credit_scope UNIQUE (id, document_id, ledger_id),
    CONSTRAINT fk_business_credit_document FOREIGN KEY(document_id, ledger_id) REFERENCES
    business_documents (id, ledger_id) ON DELETE RESTRICT,
    CONSTRAINT ck_business_credit_source_version CHECK (source_version >= 1),
    CONSTRAINT ck_business_credit_date CHECK (credit_date BETWEEN DATE '0001-01-01' AND DATE
    '9999-12-31'),
    CONSTRAINT ck_business_credit_reason CHECK (btrim(reason) <> '')
);

CREATE INDEX ix_business_credits_document ON business_credits (document_id, created_at, id);

CREATE INDEX ix_business_credits_ledger_created ON business_credits (ledger_id, created_at, id);

CREATE TABLE business_credit_lines (
    id UUID DEFAULT gen_random_uuid() NOT NULL,
    credit_id UUID NOT NULL,
    document_id UUID NOT NULL,
    source_line_id UUID NOT NULL,
    source_operation_id UUID NOT NULL,
    line_no INTEGER NOT NULL,
    net_amount NUMERIC(38, 18) NOT NULL,
    tax_amount NUMERIC(38, 18) NOT NULL,
    total_amount NUMERIC(38, 18) NOT NULL,
    recognition_date DATE NOT NULL,
    operation_id UUID NOT NULL,
    journal_id UUID NOT NULL,
    ledger_id UUID NOT NULL,
    version INTEGER DEFAULT '1' NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    PRIMARY KEY (id),
    CONSTRAINT uq_business_credit_line_id_ledger UNIQUE (id, ledger_id),
    CONSTRAINT fk_business_credit_line_ledger FOREIGN KEY(ledger_id) REFERENCES ledgers (id) ON
    DELETE RESTRICT,
    CONSTRAINT ck_business_credit_line_version CHECK (version = 1),
    CONSTRAINT uq_business_credit_line_operation UNIQUE (operation_id),
    CONSTRAINT uq_business_credit_line_journal UNIQUE (journal_id),
    CONSTRAINT fk_business_credit_line_journal FOREIGN KEY(journal_id, operation_id, ledger_id)
    REFERENCES journals (id, operation_id, ledger_id) DEFERRABLE INITIALLY DEFERRED,
    CONSTRAINT uq_business_credit_line_position UNIQUE (credit_id, line_no),
    CONSTRAINT uq_business_credit_line_source UNIQUE (credit_id, source_line_id),
    CONSTRAINT fk_business_credit_line_group FOREIGN KEY(credit_id, document_id, ledger_id)
    REFERENCES business_credits (id, document_id, ledger_id) ON DELETE RESTRICT,
    CONSTRAINT fk_business_credit_line_source FOREIGN KEY(source_line_id, document_id,
    ledger_id) REFERENCES business_document_lines (id, document_id, ledger_id) ON DELETE
    RESTRICT,
    CONSTRAINT fk_business_credit_line_original FOREIGN KEY(source_operation_id, ledger_id)
    REFERENCES financial_operations (id, ledger_id) ON DELETE RESTRICT,
    CONSTRAINT ck_business_credit_line_position CHECK (line_no BETWEEN 1 AND 200),
    CONSTRAINT ck_business_credit_line_amounts CHECK (net_amount >= 0 AND tax_amount >= 0 AND
    total_amount > 0 AND total_amount < 100000000000000000000 AND net_amount + tax_amount =
    total_amount),
    CONSTRAINT ck_business_credit_line_date CHECK (recognition_date BETWEEN DATE '0001-01-01'
    AND DATE '9999-12-31')
);

CREATE INDEX ix_business_credit_lines_source ON business_credit_lines (source_line_id);
"""

_GUARDS = r"""
CREATE FUNCTION coinpup_guard_business_credit() RETURNS trigger
LANGUAGE plpgsql SET search_path=pg_catalog AS $$
BEGIN
    IF TG_OP <> 'INSERT' THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='Business credit history is immutable',
            CONSTRAINT='ck_business_credit_immutable';
    END IF;
    PERFORM pg_advisory_xact_lock(18945999704708432);
    IF current_setting('transaction_isolation') <> 'read committed' THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='Business credit isolation is invalid',
            CONSTRAINT='ck_business_credit_isolation';
    END IF;
    RETURN NEW;
END; $$;

CREATE FUNCTION coinpup_validate_business_credit(credit_identifier uuid) RETURNS void
LANGUAGE plpgsql SET search_path=pg_catalog AS $$
BEGIN
    RAISE EXCEPTION USING ERRCODE='23514',
        MESSAGE='Business credit requires dedicated validation',
        CONSTRAINT='ck_business_credit_shape';
END; $$;

CREATE FUNCTION coinpup_check_business_credit() RETURNS trigger
LANGUAGE plpgsql SET search_path=pg_catalog AS $$
BEGIN
    IF TG_TABLE_NAME='business_credits' THEN
        PERFORM public.coinpup_validate_business_credit(NEW.id);
    ELSE
        PERFORM public.coinpup_validate_business_credit(NEW.credit_id);
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
            "FOR EACH ROW EXECUTE FUNCTION coinpup_guard_business_credit()"
        )
        op.execute(
            f"CREATE CONSTRAINT TRIGGER tr_{table}_complete AFTER INSERT ON {table} "
            "DEFERRABLE INITIALLY DEFERRED FOR EACH ROW "
            "EXECUTE FUNCTION coinpup_check_business_credit()"
        )
        op.execute(_CHANGE_TRIGGER_SQL[table])


def downgrade():
    op.execute("SELECT pg_advisory_xact_lock(18945999704708432)")
    op.execute("LOCK TABLE " + ", ".join(_TABLES) + " IN ACCESS EXCLUSIVE MODE")
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM public.business_credits)
               OR EXISTS (SELECT 1 FROM public.business_credit_lines)
               OR EXISTS (SELECT 1 FROM public.change_log WHERE entity_type IN
                   ('business_credits','business_credit_lines')) THEN
                RAISE EXCEPTION USING ERRCODE='23514',
                    MESSAGE='Business credit history prevents downgrade',
                    CONSTRAINT='ck_business_credit_downgrade';
            END IF;
        END; $$;
    """)
    for table in reversed(_TABLES):
        op.drop_table(table)
    for function in (
        "coinpup_check_business_credit()",
        "coinpup_validate_business_credit(uuid)",
        "coinpup_guard_business_credit()",
    ):
        op.execute("DROP FUNCTION " + function)
    _change_types(_OLD_ENTITY_TYPES)
