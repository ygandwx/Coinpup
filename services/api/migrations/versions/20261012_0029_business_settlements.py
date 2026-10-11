"""Immutable settlement storage; dedicated posting kinds activate only after full checks."""

from alembic import op

revision = "20261012_0029"
down_revision = "20261011_0028"
branch_labels = None
depends_on = None

_TABLES = ("business_payments", "business_allocations", "business_allocation_lines")
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
)

_ENTITY_TYPES = ", ".join(repr(value) for value in _OLD_ENTITY_TYPES + _TABLES)
_CHANGE_TRIGGER_SQL = {
    table: f"CREATE TRIGGER trg_{table}_change_log AFTER INSERT OR UPDATE ON {table} "
    "FOR EACH ROW EXECUTE FUNCTION coinpup_record_change()"
    for table in _TABLES
}

_SCHEMA_SQL = """
CREATE TABLE business_payments (
    party_id UUID NOT NULL,
    account_id UUID NOT NULL,
    asset_id VARCHAR(200) NOT NULL,
    direction VARCHAR(8) NOT NULL,
    amount NUMERIC(38, 18) NOT NULL,
    transaction_date DATE NOT NULL,
    description VARCHAR(2000) NOT NULL,
    fees JSONB NOT NULL,
    operation_id UUID NOT NULL,
    journal_id UUID NOT NULL,
    initial_allocation_id UUID,
    created_by UUID NOT NULL,
    idempotency_key VARCHAR(128) NOT NULL,
    id UUID NOT NULL,
    ledger_id UUID NOT NULL,
    version INTEGER DEFAULT '1' NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    PRIMARY KEY (id),
    CONSTRAINT uq_business_payment_id_ledger UNIQUE (id, ledger_id),
    CONSTRAINT fk_business_payment_ledger FOREIGN KEY(ledger_id) REFERENCES ledgers (id) ON
    DELETE RESTRICT,
    CONSTRAINT ck_business_payment_version CHECK (version = 1),
    CONSTRAINT uq_business_payment_operation UNIQUE (operation_id),
    CONSTRAINT uq_business_payment_journal UNIQUE (journal_id),
    CONSTRAINT fk_business_payment_journal FOREIGN KEY(journal_id, operation_id, ledger_id)
    REFERENCES journals (id, operation_id, ledger_id) DEFERRABLE INITIALLY DEFERRED,
    CONSTRAINT uq_business_payment_key UNIQUE (ledger_id, idempotency_key),
    CONSTRAINT fk_business_payment_owner FOREIGN KEY(ledger_id, created_by) REFERENCES ledgers
    (id, owner_id) ON DELETE RESTRICT,
    CONSTRAINT fk_business_payment_receipt FOREIGN KEY(ledger_id, idempotency_key) REFERENCES
    command_receipts (ledger_id, key) DEFERRABLE INITIALLY DEFERRED,
    CONSTRAINT fk_business_payment_party FOREIGN KEY(party_id, ledger_id) REFERENCES
    business_parties (id, ledger_id) ON DELETE RESTRICT,
    CONSTRAINT fk_business_payment_account_asset FOREIGN KEY(account_id, asset_id, ledger_id)
    REFERENCES account_assets (account_id, asset_id, ledger_id) ON DELETE RESTRICT,
    CONSTRAINT ck_business_payment_direction CHECK (direction IN ('receipt', 'payout')),
    CONSTRAINT ck_business_payment_amount CHECK (amount > 0 AND amount < 100000000000000000000),
    CONSTRAINT ck_business_payment_date CHECK (transaction_date BETWEEN DATE '0001-01-01' AND
    DATE '9999-12-31'),
    CONSTRAINT ck_business_payment_fees CHECK (CASE WHEN jsonb_typeof(fees) = 'array' THEN
    jsonb_array_length(fees) <= 20 ELSE false END)
);

CREATE INDEX ix_business_payments_ledger_created ON business_payments (ledger_id, created_at,
    id);

CREATE TABLE business_allocations (
    payment_id UUID NOT NULL,
    allocation_date DATE NOT NULL,
    created_by UUID NOT NULL,
    idempotency_key VARCHAR(128) NOT NULL,
    id UUID NOT NULL,
    ledger_id UUID NOT NULL,
    version INTEGER DEFAULT '1' NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    PRIMARY KEY (id),
    CONSTRAINT uq_business_allocation_id_ledger UNIQUE (id, ledger_id),
    CONSTRAINT fk_business_allocation_ledger FOREIGN KEY(ledger_id) REFERENCES ledgers (id) ON
    DELETE RESTRICT,
    CONSTRAINT ck_business_allocation_version CHECK (version = 1),
    CONSTRAINT uq_business_allocation_key UNIQUE (ledger_id, idempotency_key),
    CONSTRAINT fk_business_allocation_owner FOREIGN KEY(ledger_id, created_by) REFERENCES
    ledgers (id, owner_id) ON DELETE RESTRICT,
    CONSTRAINT fk_business_allocation_receipt FOREIGN KEY(ledger_id, idempotency_key) REFERENCES
    command_receipts (ledger_id, key) DEFERRABLE INITIALLY DEFERRED,
    CONSTRAINT uq_business_allocation_scope UNIQUE (id, payment_id, ledger_id),
    CONSTRAINT fk_business_allocation_payment FOREIGN KEY(payment_id, ledger_id) REFERENCES
    business_payments (id, ledger_id) ON DELETE RESTRICT,
    CONSTRAINT ck_business_allocation_date CHECK (allocation_date BETWEEN DATE '0001-01-01' AND
    DATE '9999-12-31')
);

CREATE INDEX ix_business_allocations_ledger_created ON business_allocations (ledger_id,
    created_at, id);

CREATE INDEX ix_business_allocations_payment ON business_allocations (payment_id, created_at,
    id);

CREATE TABLE business_allocation_lines (
    id UUID DEFAULT gen_random_uuid() NOT NULL,
    allocation_id UUID NOT NULL,
    payment_id UUID NOT NULL,
    document_id UUID NOT NULL,
    document_line_id UUID NOT NULL,
    line_no INTEGER NOT NULL,
    payment_amount NUMERIC(38, 18) NOT NULL,
    document_amount NUMERIC(38, 18) NOT NULL,
    operation_id UUID NOT NULL,
    journal_id UUID NOT NULL,
    ledger_id UUID NOT NULL,
    version INTEGER DEFAULT '1' NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    PRIMARY KEY (id),
    CONSTRAINT uq_business_allocation_line_id_ledger UNIQUE (id, ledger_id),
    CONSTRAINT fk_business_allocation_line_ledger FOREIGN KEY(ledger_id) REFERENCES ledgers (id)
    ON DELETE RESTRICT,
    CONSTRAINT ck_business_allocation_line_version CHECK (version = 1),
    CONSTRAINT uq_business_allocation_line_operation UNIQUE (operation_id),
    CONSTRAINT uq_business_allocation_line_journal UNIQUE (journal_id),
    CONSTRAINT fk_business_allocation_line_journal FOREIGN KEY(journal_id, operation_id,
    ledger_id) REFERENCES journals (id, operation_id, ledger_id) DEFERRABLE INITIALLY DEFERRED,
    CONSTRAINT uq_business_allocation_line_position UNIQUE (allocation_id, line_no),
    CONSTRAINT uq_business_allocation_line_source UNIQUE (allocation_id, document_line_id),
    CONSTRAINT fk_business_allocation_line_group FOREIGN KEY(allocation_id, payment_id,
    ledger_id) REFERENCES business_allocations (id, payment_id, ledger_id) ON DELETE RESTRICT,
    CONSTRAINT fk_business_allocation_line_source FOREIGN KEY(document_line_id, document_id,
    ledger_id) REFERENCES business_document_lines (id, document_id, ledger_id) ON DELETE
    RESTRICT,
    CONSTRAINT ck_business_allocation_line_position CHECK (line_no BETWEEN 1 AND 200),
    CONSTRAINT ck_business_allocation_line_amounts CHECK (payment_amount > 0 AND payment_amount
    < 100000000000000000000 AND document_amount > 0 AND document_amount < 100000000000000000000)
);

CREATE INDEX ix_business_allocation_lines_payment ON business_allocation_lines (payment_id);

CREATE INDEX ix_business_allocation_lines_source ON business_allocation_lines
    (document_line_id);

ALTER TABLE business_payments ADD CONSTRAINT fk_business_payment_initial_allocation FOREIGN
    KEY(initial_allocation_id, id, ledger_id) REFERENCES business_allocations (id, payment_id,
    ledger_id) DEFERRABLE INITIALLY DEFERRED;
"""

_GUARD_SQL = """
CREATE FUNCTION coinpup_guard_business_settlement() RETURNS trigger
LANGUAGE plpgsql SET search_path=pg_catalog AS $$
BEGIN
    IF TG_OP <> 'INSERT' THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='Business settlement history is immutable',
            CONSTRAINT='ck_business_settlement_immutable';
    END IF;
    PERFORM pg_advisory_xact_lock(18945999704708432);
    RETURN NEW;
END; $$;

CREATE FUNCTION coinpup_validate_business_payment(payment_identifier uuid) RETURNS void
LANGUAGE plpgsql SET search_path=pg_catalog AS $$
DECLARE operation_kind text;
BEGIN
    SELECT f.kind INTO operation_kind FROM public.business_payments p
      LEFT JOIN public.financial_operations f ON f.id=p.operation_id AND f.ledger_id=p.ledger_id
      WHERE p.id=payment_identifier;
    IF operation_kind IS DISTINCT FROM 'payment' THEN
        RAISE EXCEPTION USING ERRCODE='23514',
            MESSAGE='Payment requires its dedicated financial operation',
            CONSTRAINT='ck_business_payment_shape';
    END IF;
END; $$;

CREATE FUNCTION coinpup_validate_business_allocation(allocation_identifier uuid) RETURNS void
LANGUAGE plpgsql SET search_path=pg_catalog AS $$
DECLARE members integer; invalid boolean;
BEGIN
    SELECT count(*), coalesce(bool_or(f.kind IS DISTINCT FROM 'allocation'), false)
      INTO members, invalid FROM public.business_allocation_lines l
      LEFT JOIN public.financial_operations f ON f.id=l.operation_id AND f.ledger_id=l.ledger_id
      WHERE l.allocation_id=allocation_identifier;
    IF NOT EXISTS (SELECT 1 FROM public.business_allocations WHERE id=allocation_identifier)
       OR members NOT BETWEEN 1 AND 200 OR invalid THEN
        RAISE EXCEPTION USING ERRCODE='23514',
            MESSAGE='Allocation requires complete dedicated financial operations',
            CONSTRAINT='ck_business_allocation_shape';
    END IF;
END; $$;

CREATE FUNCTION coinpup_check_business_settlement() RETURNS trigger
LANGUAGE plpgsql SET search_path=pg_catalog AS $$
BEGIN
    IF TG_TABLE_NAME='business_payments' THEN
        PERFORM public.coinpup_validate_business_payment(NEW.id);
    ELSIF TG_TABLE_NAME='business_allocations' THEN
        PERFORM public.coinpup_validate_business_allocation(NEW.id);
    ELSE
        PERFORM public.coinpup_validate_business_allocation(NEW.allocation_id);
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
    op.execute(_GUARD_SQL)
    _change_types(_OLD_ENTITY_TYPES + _TABLES)
    for table in _TABLES:
        op.execute(
            f"CREATE TRIGGER tr_{table}_guard BEFORE INSERT OR UPDATE OR DELETE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION coinpup_guard_business_settlement()"
        )
        op.execute(
            f"CREATE CONSTRAINT TRIGGER tr_{table}_complete AFTER INSERT ON {table} "
            "DEFERRABLE INITIALLY DEFERRED FOR EACH ROW "
            "EXECUTE FUNCTION coinpup_check_business_settlement()"
        )
        op.execute(_CHANGE_TRIGGER_SQL[table])


def downgrade():
    op.execute("SELECT pg_advisory_xact_lock(18945999704708432)")
    op.execute("LOCK TABLE " + ", ".join(_TABLES) + " IN ACCESS EXCLUSIVE MODE")
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM public.business_payments)
               OR EXISTS (SELECT 1 FROM public.business_allocations)
               OR EXISTS (SELECT 1 FROM public.business_allocation_lines)
               OR EXISTS (SELECT 1 FROM public.change_log WHERE entity_type IN
                   ('business_payments','business_allocations','business_allocation_lines')) THEN
                RAISE EXCEPTION USING ERRCODE='23514',
                    MESSAGE='Business settlement history prevents downgrade',
                    CONSTRAINT='ck_business_settlement_downgrade';
            END IF;
        END; $$;
    """)
    op.drop_constraint(
        "fk_business_payment_initial_allocation", "business_payments", type_="foreignkey"
    )
    for table in reversed(_TABLES):
        op.drop_table(table)
    for function in (
        "coinpup_check_business_settlement()",
        "coinpup_validate_business_allocation(uuid)",
        "coinpup_validate_business_payment(uuid)",
        "coinpup_guard_business_settlement()",
    ):
        op.execute("DROP FUNCTION " + function)
    _change_types(_OLD_ENTITY_TYPES)
