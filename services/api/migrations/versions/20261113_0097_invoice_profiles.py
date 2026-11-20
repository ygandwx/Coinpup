"""Scoped current company Invoice configuration, without issuance or numbering allocation."""

from alembic import op

revision = "20261113_0097"
down_revision = "20261120_0131"
branch_labels = None
depends_on = None

_TABLES = ("business_invoice_profiles",)
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
)
_ENTITY_TYPES = ", ".join(repr(value) for value in _OLD_ENTITY_TYPES + _TABLES)
_CHANGE_TRIGGER_SQL = {
    table: f"CREATE TRIGGER trg_{table}_change_log AFTER INSERT OR UPDATE ON {table} "
    "FOR EACH ROW EXECUTE FUNCTION coinpup_record_change()"
    for table in _TABLES
}

_SCHEMA_SQL = r"""
CREATE TABLE business_invoice_profiles (
    name VARCHAR(160) NOT NULL,
    number_prefix VARCHAR(40) NOT NULL,
    minimum_digits INTEGER NOT NULL,
    issuer_extra_text VARCHAR(2000),
    payment_instructions VARCHAR(2000),
    footer VARCHAR(2000),
    logo_file_id UUID,
    logo_file_version INTEGER,
    id UUID NOT NULL,
    ledger_id UUID NOT NULL,
    version INTEGER DEFAULT '1' NOT NULL,
    archived BOOLEAN DEFAULT false NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    PRIMARY KEY (id),
    CONSTRAINT uq_business_invoice_profiles_id_ledger UNIQUE (id, ledger_id),
    CONSTRAINT fk_business_invoice_profiles_ledger FOREIGN KEY(ledger_id)
        REFERENCES ledgers (id) ON DELETE RESTRICT,
    CONSTRAINT ck_business_invoice_profiles_version CHECK (version > 0),
    CONSTRAINT fk_invoice_profile_logo_ledger FOREIGN KEY(logo_file_id, ledger_id)
        REFERENCES stored_files (id, ledger_id) ON DELETE RESTRICT,
    CONSTRAINT ck_invoice_profile_name CHECK (btrim(name) <> ''),
    CONSTRAINT ck_invoice_profile_prefix CHECK (number_prefix = btrim(number_prefix)
        AND number_prefix !~ '[[:cntrl:]]'),
    CONSTRAINT ck_invoice_profile_digits CHECK (minimum_digits BETWEEN 1 AND 20),
    CONSTRAINT ck_invoice_profile_logo_review CHECK ((logo_file_id IS NULL
        AND logo_file_version IS NULL) OR
        (logo_file_id IS NOT NULL AND logo_file_version IS NOT NULL AND logo_file_version > 0)),
    CONSTRAINT ck_invoice_profile_timestamps CHECK (updated_at >= created_at)
);
CREATE INDEX ix_business_invoice_profiles_ledger_id ON business_invoice_profiles (ledger_id);
"""

_GUARD_SQL = r"""
CREATE FUNCTION coinpup_guard_invoice_profile() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog AS $$
DECLARE mutable_fields text[] := ARRAY[
    'version','archived','updated_at','name','number_prefix','minimum_digits',
    'issuer_extra_text','payment_instructions','footer','logo_file_id','logo_file_version'
];
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='Invoice configuration is retained',
            CONSTRAINT='ck_invoice_profile_identity';
    END IF;
    IF (TG_OP='INSERT' AND (NEW.version<>1 OR NEW.archived)) OR
       (TG_OP='UPDATE' AND (NEW.version::bigint<>OLD.version::bigint+1 OR
        (to_jsonb(NEW)-mutable_fields) IS DISTINCT FROM (to_jsonb(OLD)-mutable_fields) OR
        NEW.updated_at<OLD.updated_at)) THEN
        RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='Invoice configuration identity is fixed',
            CONSTRAINT='ck_invoice_profile_identity';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM public.ledgers l JOIN public.entities e ON e.id=l.entity_id
        WHERE l.id=NEW.ledger_id AND e.kind='company') THEN
        RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='Invoice configuration requires a company',
            CONSTRAINT='ck_invoice_profile_company';
    END IF;
    IF NEW.logo_file_id IS NOT NULL AND (TG_OP='INSERT' OR
        (NEW.logo_file_id,NEW.logo_file_version) IS DISTINCT FROM
        (OLD.logo_file_id,OLD.logo_file_version)) THEN
        IF NOT EXISTS (SELECT 1 FROM public.stored_files f
            WHERE (f.id,f.ledger_id,f.version)=
                (NEW.logo_file_id,NEW.ledger_id,NEW.logo_file_version)
            AND NOT f.archived AND f.detected_media_type IN ('image/png','image/jpeg','image/webp'))
        THEN
            RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='Invoice Logo review is inconsistent',
                CONSTRAINT='ck_invoice_profile_logo_scope';
        END IF;
    END IF;
    RETURN NEW;
END; $$;
"""


def upgrade():
    op.execute("SELECT pg_advisory_xact_lock(18945999704708432)")
    op.execute(_SCHEMA_SQL)
    op.execute(_GUARD_SQL)
    op.execute(
        "CREATE TRIGGER tr_invoice_profile_identity BEFORE INSERT OR UPDATE OR DELETE "
        "ON business_invoice_profiles FOR EACH ROW EXECUTE FUNCTION coinpup_guard_invoice_profile()"
    )
    op.execute("ALTER TABLE change_log DROP CONSTRAINT ck_change_log_entity_type")
    op.execute(
        "ALTER TABLE change_log ADD CONSTRAINT ck_change_log_entity_type "
        f"CHECK (entity_type IN ({_ENTITY_TYPES}))"
    )
    op.execute(_CHANGE_TRIGGER_SQL["business_invoice_profiles"])


def downgrade():
    op.execute("SELECT pg_advisory_xact_lock(18945999704708432)")
    op.execute("LOCK TABLE business_invoice_profiles IN ACCESS EXCLUSIVE MODE")
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM public.business_invoice_profiles) OR EXISTS (
                SELECT 1 FROM public.change_log WHERE entity_type='business_invoice_profiles'
            ) THEN
                RAISE EXCEPTION USING ERRCODE='23514',
                    MESSAGE='Invoice configuration history exists',
                    CONSTRAINT='ck_invoice_profile_downgrade';
            END IF;
        END; $$;
    """)
    op.execute("DROP TABLE business_invoice_profiles")
    op.execute("DROP FUNCTION coinpup_guard_invoice_profile()")
    old_types = ", ".join(repr(value) for value in _OLD_ENTITY_TYPES)
    op.execute("ALTER TABLE change_log DROP CONSTRAINT ck_change_log_entity_type")
    op.execute(
        "ALTER TABLE change_log ADD CONSTRAINT ck_change_log_entity_type "
        f"CHECK (entity_type IN ({old_types}))"
    )
