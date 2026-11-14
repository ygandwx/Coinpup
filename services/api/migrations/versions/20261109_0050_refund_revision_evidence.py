"""Add ordered refund revision evidence without opening any financial revision writes."""

from alembic import op

revision = "20261109_0050"
down_revision = "20261109_0049"
branch_labels = None
depends_on = None

_TABLES = ("business_refund_revision_returns",)
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
)
_ENTITY_TYPES = ", ".join(repr(value) for value in _OLD_ENTITY_TYPES + _TABLES)
_CHANGE_TRIGGER_SQL = {
    table: f"CREATE TRIGGER trg_{table}_change_log AFTER INSERT OR UPDATE ON {table} "
    "FOR EACH ROW EXECUTE FUNCTION coinpup_record_change()"
    for table in _TABLES
}

_SCHEMA_SQL = """
CREATE TABLE business_refund_revision_returns (
    revision_id UUID NOT NULL,
    refund_id UUID NOT NULL,
    payment_id UUID NOT NULL,
    return_id UUID NOT NULL,
    return_version INTEGER NOT NULL,
    line_no INTEGER NOT NULL,
    id UUID NOT NULL,
    ledger_id UUID NOT NULL,
    version INTEGER DEFAULT '1' NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    PRIMARY KEY (id),
    CONSTRAINT uq_refund_revision_return_id_ledger UNIQUE (id, ledger_id),
    CONSTRAINT fk_refund_revision_return_ledger FOREIGN KEY(ledger_id) REFERENCES ledgers (id) ON
        DELETE RESTRICT,
    CONSTRAINT ck_refund_revision_return_version CHECK (version = 1),
    CONSTRAINT uq_refund_revision_return_position UNIQUE (revision_id, line_no),
    CONSTRAINT uq_refund_revision_return_source UNIQUE (revision_id, return_id),
    CONSTRAINT fk_refund_revision_return_parent FOREIGN KEY(revision_id, refund_id, payment_id,
        ledger_id) REFERENCES business_refund_revisions (id, refund_id, payment_id, ledger_id) ON
        DELETE RESTRICT,
    CONSTRAINT fk_refund_revision_return_source FOREIGN KEY(return_id, payment_id, ledger_id)
        REFERENCES business_allocation_returns (id, payment_id, ledger_id) ON DELETE RESTRICT,
    CONSTRAINT ck_refund_revision_return_source_version CHECK (return_version = 1),
    CONSTRAINT ck_refund_revision_return_position CHECK (line_no BETWEEN 1 AND 200)
);
CREATE INDEX ix_refund_revision_returns_source ON business_refund_revision_returns (ledger_id,
        return_id);
"""

_GUARDS = r"""
CREATE FUNCTION coinpup_check_refund_revision_return() RETURNS trigger
LANGUAGE plpgsql SET search_path=pg_catalog AS $$
DECLARE last_position integer; total integer;
BEGIN
    SELECT max(line_no), count(*) INTO last_position, total
    FROM public.business_refund_revision_returns WHERE revision_id=NEW.revision_id;
    IF last_position IS DISTINCT FROM total OR
       (SELECT action FROM public.business_refund_revisions WHERE id=NEW.revision_id)
           IS DISTINCT FROM 'correct' THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='Refund replacement evidence is invalid',
            CONSTRAINT='ck_refund_revision_evidence';
    END IF;
    PERFORM public.coinpup_validate_refund_revision(NEW.revision_id);
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
        "CREATE TRIGGER tr_refund_revision_returns_guard BEFORE INSERT OR UPDATE OR DELETE "
        "ON business_refund_revision_returns FOR EACH ROW "
        "EXECUTE FUNCTION coinpup_guard_refund_revision()"
    )
    op.execute(
        "CREATE CONSTRAINT TRIGGER tr_refund_revision_returns_complete AFTER INSERT "
        "ON business_refund_revision_returns DEFERRABLE INITIALLY DEFERRED FOR EACH ROW "
        "EXECUTE FUNCTION coinpup_check_refund_revision_return()"
    )
    op.execute(_CHANGE_TRIGGER_SQL["business_refund_revision_returns"])


def downgrade():
    op.execute("SELECT pg_advisory_xact_lock(18945999704708432)")
    op.execute(
        "LOCK TABLE public.business_refund_revisions, public.business_refund_revision_returns "
        "IN ACCESS EXCLUSIVE MODE"
    )
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM public.business_refund_revisions)
                OR EXISTS (SELECT 1 FROM public.business_refund_revision_returns)
                OR EXISTS (SELECT 1 FROM public.change_log
                    WHERE entity_type IN
                        ('business_refund_revisions','business_refund_revision_returns')
                        OR (entity_type='business_refunds' AND entity_version>1)) THEN
                RAISE EXCEPTION USING ERRCODE='23514',
                    MESSAGE='Refund revision history prevents downgrade',
                    CONSTRAINT='ck_refund_revision_downgrade';
            END IF;
        END; $$;
    """)
    op.drop_table("business_refund_revision_returns")
    op.execute("DROP FUNCTION coinpup_check_refund_revision_return()")
    _change_types(_OLD_ENTITY_TYPES)
