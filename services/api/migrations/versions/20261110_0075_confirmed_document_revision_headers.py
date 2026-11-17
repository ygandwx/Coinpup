"""Register immutable confirmed-document revision headers before opening complete group writes."""

from alembic import op

revision = "20261110_0075"
down_revision = "20261109_0074"
branch_labels = None
depends_on = None


_TABLES = ("business_document_revisions",)
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
)
_ENTITY_TYPES = ", ".join(repr(value) for value in _OLD_ENTITY_TYPES + _TABLES)
_CHANGE_TRIGGER_SQL = {
    table: f"CREATE TRIGGER trg_{table}_change_log AFTER INSERT OR UPDATE ON {table} "
    "FOR EACH ROW EXECUTE FUNCTION coinpup_record_change()"
    for table in _TABLES
}

_SCHEMA_SQL = r"""
CREATE TABLE business_document_revisions (
    confirmation_id UUID NOT NULL,
    document_id UUID NOT NULL,
    previous_version INTEGER NOT NULL,
    document_version INTEGER NOT NULL,
    previous_revision_id UUID,
    action VARCHAR(7) NOT NULL,
    reason VARCHAR(1000) NOT NULL,
    party_id UUID,
    asset_id VARCHAR(200),
    issue_date DATE,
    due_date DATE,
    notes VARCHAR(2000),
    party_snapshot JSONB,
    created_by UUID NOT NULL,
    idempotency_key VARCHAR(128) NOT NULL,
    id UUID NOT NULL,
    ledger_id UUID NOT NULL,
    version INTEGER DEFAULT '1' NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    PRIMARY KEY (id),
    CONSTRAINT uq_business_document_revision_id_ledger UNIQUE (id, ledger_id),
    CONSTRAINT fk_business_document_revision_ledger FOREIGN KEY(ledger_id) REFERENCES ledgers (id)
        ON DELETE RESTRICT,
    CONSTRAINT ck_business_document_revision_version CHECK (version = 1),
    CONSTRAINT uq_business_document_revision_key UNIQUE (ledger_id, idempotency_key),
    CONSTRAINT fk_business_document_revision_owner FOREIGN KEY(ledger_id, created_by) REFERENCES
        ledgers (id, owner_id) ON DELETE RESTRICT,
    CONSTRAINT fk_business_document_revision_receipt FOREIGN KEY(ledger_id, idempotency_key)
        REFERENCES command_receipts (ledger_id, key) DEFERRABLE INITIALLY DEFERRED,
    CONSTRAINT uq_document_revision_version UNIQUE (document_id, document_version),
    CONSTRAINT uq_document_revision_version_scope UNIQUE (id, confirmation_id, document_id,
        ledger_id, document_version),
    CONSTRAINT fk_document_revision_confirmation FOREIGN KEY(confirmation_id, document_id,
        ledger_id) REFERENCES business_confirmations (id, document_id, ledger_id) ON DELETE
        RESTRICT,
    CONSTRAINT fk_document_revision_previous FOREIGN KEY(previous_revision_id, confirmation_id,
        document_id, ledger_id, previous_version) REFERENCES business_document_revisions (id,
        confirmation_id, document_id, ledger_id, document_version) ON DELETE RESTRICT,
    CONSTRAINT fk_document_revision_party FOREIGN KEY(party_id, ledger_id) REFERENCES
        business_parties (id, ledger_id) ON DELETE RESTRICT,
    CONSTRAINT fk_document_revision_asset FOREIGN KEY(asset_id) REFERENCES assets (asset_id) ON
        DELETE RESTRICT,
    CONSTRAINT ck_document_revision_versions CHECK (previous_version BETWEEN 2 AND 2147483646 AND
        document_version - 1::bigint = previous_version),
    CONSTRAINT ck_document_revision_previous CHECK (previous_revision_id IS NULL OR
        previous_revision_id <> id),
    CONSTRAINT ck_document_revision_action CHECK (action IN ('correct', 'cancel')),
    CONSTRAINT ck_document_revision_reason CHECK (reason ~ '^[^[:space:]]' AND reason ~
        '[^[:space:]]$'),
    CONSTRAINT ck_document_revision_replacement CHECK ((action = 'correct' AND party_id IS NOT
        NULL AND asset_id IS NOT NULL AND issue_date IS NOT NULL AND issue_date BETWEEN DATE
        '0001-01-01' AND DATE '9999-12-31' AND (due_date IS NULL OR due_date BETWEEN DATE
        '0001-01-01' AND DATE '9999-12-31') AND party_snapshot IS NOT NULL AND
        jsonb_typeof(party_snapshot) = 'object') OR (action = 'cancel' AND party_id IS NULL AND
        asset_id IS NULL AND issue_date IS NULL AND due_date IS NULL AND notes IS NULL AND
        party_snapshot IS NULL))
);
"""

_GUARDS = r"""
CREATE FUNCTION coinpup_guard_document_revision() RETURNS trigger
LANGUAGE plpgsql SET search_path=pg_catalog AS $$
DECLARE initial_version bigint;
BEGIN
    IF TG_OP <> 'INSERT' THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='Document revision history is immutable',
            CONSTRAINT='ck_document_revision_immutable';
    END IF;
    PERFORM pg_advisory_xact_lock(18945999704708432);
    IF current_setting('transaction_isolation') <> 'read committed' THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='Invalid document revision isolation',
            CONSTRAINT='ck_document_revision_isolation';
    END IF;
    IF NEW.previous_revision_id IS NULL THEN
        SELECT c.source_version::bigint + 1 INTO initial_version
        FROM public.business_confirmations c
        WHERE (c.id,c.document_id,c.ledger_id)=(NEW.confirmation_id,NEW.document_id,NEW.ledger_id);
        IF FOUND AND NEW.previous_version <> initial_version THEN
            RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='Invalid initial document version',
                CONSTRAINT='ck_document_revision_origin_version';
        END IF;
    ELSIF EXISTS (
        SELECT 1 FROM public.business_document_revisions h
        WHERE (h.id,h.document_id,h.ledger_id)=
            (NEW.previous_revision_id,NEW.document_id,NEW.ledger_id)
            AND h.action='cancel'
    ) THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='A cancelled document is terminal',
            CONSTRAINT='ck_document_revision_terminal';
    END IF;
    RETURN NEW;
END; $$;

CREATE FUNCTION coinpup_check_document_revision() RETURNS trigger
LANGUAGE plpgsql SET search_path=pg_catalog AS $$
BEGIN
    RAISE EXCEPTION USING ERRCODE='23514',
        MESSAGE='A document revision requires complete confirmed facts and accruals',
        CONSTRAINT='ck_document_revision_complete';
END; $$;
"""


def _change_types(types):
    op.drop_constraint("ck_change_log_entity_type", "change_log", type_="check")
    op.create_check_constraint(
        "ck_change_log_entity_type",
        "change_log",
        "entity_type IN (" + ", ".join(repr(item) for item in types) + ")",
    )


def upgrade():
    op.execute("SELECT pg_advisory_xact_lock(18945999704708432)")
    op.execute(_SCHEMA_SQL)
    op.execute(_GUARDS)
    _change_types(_OLD_ENTITY_TYPES + _TABLES)
    op.execute(
        "CREATE TRIGGER tr_document_revisions_guard BEFORE INSERT OR UPDATE OR DELETE "
        "ON business_document_revisions FOR EACH ROW "
        "EXECUTE FUNCTION coinpup_guard_document_revision()"
    )
    op.execute(
        "CREATE CONSTRAINT TRIGGER tr_document_revisions_complete AFTER INSERT "
        "ON business_document_revisions DEFERRABLE INITIALLY DEFERRED FOR EACH ROW "
        "EXECUTE FUNCTION coinpup_check_document_revision()"
    )
    op.execute(_CHANGE_TRIGGER_SQL["business_document_revisions"])


def downgrade():
    op.execute("SELECT pg_advisory_xact_lock(18945999704708432)")
    op.execute("LOCK TABLE public.business_document_revisions IN ACCESS EXCLUSIVE MODE")
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM public.business_document_revisions)
                OR EXISTS (SELECT 1 FROM public.change_log
                    WHERE entity_type='business_document_revisions') THEN
                RAISE EXCEPTION USING ERRCODE='23514',
                    MESSAGE='Document revision history prevents downgrade',
                    CONSTRAINT='ck_document_revision_downgrade';
            END IF;
        END; $$;
    """)
    op.drop_table("business_document_revisions")
    op.execute("DROP FUNCTION coinpup_check_document_revision()")
    op.execute("DROP FUNCTION coinpup_guard_document_revision()")
    _change_types(_OLD_ENTITY_TYPES)
