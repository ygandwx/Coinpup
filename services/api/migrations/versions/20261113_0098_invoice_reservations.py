"""Immutable scoped Invoice reservation headers; atomic allocation remains closed."""

from alembic import op

revision = "20261113_0098"
down_revision = "20261113_0097"
branch_labels = None
depends_on = None

_TABLES = ("business_invoice_reservations",)
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
    "business_return_revision_members",
    "business_document_revisions",
    "business_document_revision_lines",
    "business_document_accruals",
    "business_invoice_profiles",
)
_ENTITY_TYPES = ", ".join(repr(value) for value in _OLD_ENTITY_TYPES + _TABLES)
_CHANGE_TRIGGER_SQL = {
    table: f"CREATE TRIGGER trg_{table}_change_log AFTER INSERT OR UPDATE ON {table} "
    "FOR EACH ROW EXECUTE FUNCTION coinpup_record_change()"
    for table in _TABLES
}

_SCHEMA_SQL = r"""
CREATE TABLE business_invoice_reservations (
        document_id UUID NOT NULL,
        document_version INTEGER NOT NULL,
        configuration_id UUID NOT NULL,
        configuration_version INTEGER NOT NULL,
        previous_issuance_id UUID,
        logo_file_id UUID,
        sequence BIGINT NOT NULL,
        number VARCHAR(60) NOT NULL,
        template_version VARCHAR(64) NOT NULL,
        snapshot_json TEXT NOT NULL,
        snapshot_sha256 VARCHAR(64) NOT NULL,
        request_json TEXT NOT NULL,
        request_hash VARCHAR(64) NOT NULL,
        hash_version INTEGER NOT NULL,
        idempotency_key VARCHAR(128) NOT NULL,
        created_by UUID NOT NULL,
        id UUID NOT NULL,
        ledger_id UUID NOT NULL,
        version INTEGER DEFAULT '1' NOT NULL,
        created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
        PRIMARY KEY (id),
        CONSTRAINT uq_invoice_reservation_id_ledger UNIQUE (id, ledger_id),
        CONSTRAINT fk_invoice_reservation_ledger FOREIGN KEY(ledger_id) REFERENCES ledgers (id)
        ON DELETE RESTRICT,
        CONSTRAINT ck_invoice_reservation_version CHECK (version = 1),
        CONSTRAINT uq_invoice_reservation_key UNIQUE (ledger_id, idempotency_key),
        CONSTRAINT uq_invoice_reservation_sequence UNIQUE (ledger_id, sequence),
        CONSTRAINT uq_invoice_reservation_number UNIQUE (ledger_id, number),
        CONSTRAINT uq_invoice_reservation_version UNIQUE (ledger_id, document_id,
        document_version),
        CONSTRAINT uq_invoice_reservation_document_scope UNIQUE (id, document_id, ledger_id),
        CONSTRAINT fk_invoice_reservation_owner FOREIGN KEY(ledger_id, created_by) REFERENCES
        ledgers (id, owner_id) ON DELETE RESTRICT,
        CONSTRAINT fk_invoice_reservation_document FOREIGN KEY(document_id, ledger_id)
        REFERENCES business_documents (id, ledger_id) ON DELETE RESTRICT,
        CONSTRAINT fk_invoice_reservation_configuration FOREIGN KEY(configuration_id, ledger_id)
        REFERENCES business_invoice_profiles (id, ledger_id) ON DELETE RESTRICT,
        CONSTRAINT fk_invoice_reservation_previous FOREIGN KEY(previous_issuance_id,
        document_id, ledger_id) REFERENCES business_invoice_reservations (id, document_id,
        ledger_id) ON DELETE RESTRICT,
        CONSTRAINT fk_invoice_reservation_logo FOREIGN KEY(logo_file_id, ledger_id) REFERENCES
        stored_files (id, ledger_id) ON DELETE RESTRICT,
        CONSTRAINT ck_invoice_reservation_sequence CHECK (sequence > 0),
        CONSTRAINT ck_invoice_reservation_versions CHECK (document_version >= 2 AND
        configuration_version > 0),
        CONSTRAINT ck_invoice_reservation_number CHECK (number <> '' AND number !~
        '[[:cntrl:]]'),
        CONSTRAINT ck_invoice_reservation_previous CHECK (previous_issuance_id IS NULL OR
        previous_issuance_id <> id),
        CONSTRAINT ck_invoice_reservation_request_hash CHECK (hash_version = 2 AND request_hash
        ~ '^[0-9a-f]{64}$'),
        CONSTRAINT ck_invoice_reservation_snapshot_hash CHECK (snapshot_sha256 ~
        '^[0-9a-f]{64}$'),
        CONSTRAINT ck_invoice_reservation_template CHECK (template_version ~
        '^[a-z0-9][a-z0-9._-]{0,63}$'),
        CONSTRAINT ck_invoice_reservation_key CHECK (idempotency_key ~ '^[!-~]{1,128}$'),
        CONSTRAINT ck_invoice_reservation_request_size CHECK (octet_length(request_json) BETWEEN
        2 AND 65536),
        CONSTRAINT ck_invoice_reservation_snapshot_size CHECK (octet_length(snapshot_json)
        BETWEEN 2 AND 4259840)
);
"""

_GUARD_SQL = r"""
CREATE FUNCTION coinpup_guard_invoice_reservation() RETURNS trigger
LANGUAGE plpgsql SET search_path=pg_catalog AS $$
BEGIN
    IF TG_OP <> 'INSERT' THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='Invoice reservation history is immutable',
            CONSTRAINT='ck_invoice_reservation_immutable';
    END IF;
    PERFORM pg_advisory_xact_lock(18945999704708432);
    IF current_setting('transaction_isolation') <> 'read committed' THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='Invalid Invoice reservation isolation',
            CONSTRAINT='ck_invoice_reservation_isolation';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM public.ledgers l JOIN public.entities e ON e.id=l.entity_id
        WHERE l.id=NEW.ledger_id AND e.kind='company') THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='Invoice reservation requires a company',
            CONSTRAINT='ck_invoice_reservation_company';
    END IF;
    RETURN NEW;
END; $$;

CREATE FUNCTION coinpup_check_invoice_reservation() RETURNS trigger
LANGUAGE plpgsql SET search_path=pg_catalog AS $$
BEGIN
    RAISE EXCEPTION USING ERRCODE='23514',
        MESSAGE='Invoice reservation requires complete atomic numbering and reviewed facts',
        CONSTRAINT='ck_invoice_reservation_complete';
END; $$;
"""


def upgrade():
    op.execute("SELECT pg_advisory_xact_lock(18945999704708432)")
    op.execute(_SCHEMA_SQL)
    op.execute(_GUARD_SQL)
    op.execute(
        "CREATE TRIGGER tr_invoice_reservation_immutable BEFORE INSERT OR UPDATE OR DELETE "
        "ON business_invoice_reservations FOR EACH ROW "
        "EXECUTE FUNCTION coinpup_guard_invoice_reservation()"
    )
    op.execute(
        "CREATE CONSTRAINT TRIGGER tr_invoice_reservation_complete AFTER INSERT "
        "ON business_invoice_reservations DEFERRABLE INITIALLY DEFERRED "
        "FOR EACH ROW EXECUTE FUNCTION coinpup_check_invoice_reservation()"
    )
    op.execute("ALTER TABLE change_log DROP CONSTRAINT ck_change_log_entity_type")
    op.execute(
        "ALTER TABLE change_log ADD CONSTRAINT ck_change_log_entity_type "
        f"CHECK (entity_type IN ({_ENTITY_TYPES}))"
    )
    op.execute(_CHANGE_TRIGGER_SQL["business_invoice_reservations"])


def downgrade():
    op.execute("SELECT pg_advisory_xact_lock(18945999704708432)")
    op.execute("LOCK TABLE business_invoice_reservations IN ACCESS EXCLUSIVE MODE")
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM public.business_invoice_reservations) OR EXISTS (
                SELECT 1 FROM public.change_log WHERE entity_type='business_invoice_reservations'
            ) THEN
                RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='Invoice reservation history exists',
                    CONSTRAINT='ck_invoice_reservation_downgrade';
            END IF;
        END; $$;
    """)
    op.execute("DROP TABLE business_invoice_reservations")
    op.execute("DROP FUNCTION coinpup_check_invoice_reservation()")
    op.execute("DROP FUNCTION coinpup_guard_invoice_reservation()")
    old_types = ", ".join(repr(value) for value in _OLD_ENTITY_TYPES)
    op.execute("ALTER TABLE change_log DROP CONSTRAINT ck_change_log_entity_type")
    op.execute(
        "ALTER TABLE change_log ADD CONSTRAINT ck_change_log_entity_type "
        f"CHECK (entity_type IN ({old_types}))"
    )
