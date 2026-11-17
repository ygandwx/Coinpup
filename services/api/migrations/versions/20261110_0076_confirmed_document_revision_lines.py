"""Freeze ordered replacement lines and verify their exact pricing in database units."""

from alembic import op

revision = "20261110_0076"
down_revision = "20261110_0075"
branch_labels = None
depends_on = None


_TABLES = ("business_document_revision_lines",)
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
)
_ENTITY_TYPES = ", ".join(repr(value) for value in _OLD_ENTITY_TYPES + _TABLES)
_CHANGE_TRIGGER_SQL = {
    table: f"CREATE TRIGGER trg_{table}_change_log AFTER INSERT OR UPDATE ON {table} "
    "FOR EACH ROW EXECUTE FUNCTION coinpup_record_change()"
    for table in _TABLES
}

_SCHEMA_SQL = r"""
CREATE TABLE business_document_revision_lines (
    revision_id UUID NOT NULL,
    confirmation_id UUID NOT NULL,
    document_id UUID NOT NULL,
    document_version INTEGER NOT NULL,
    source_line_id UUID NOT NULL,
    line_no INTEGER NOT NULL,
    description VARCHAR(2000) NOT NULL,
    quantity VARCHAR(39) NOT NULL,
    unit_price VARCHAR(40) NOT NULL,
    discount_amount VARCHAR(40) NOT NULL,
    tax_rate_percent VARCHAR(39) NOT NULL,
    category_id UUID NOT NULL,
    category_kind VARCHAR(7) NOT NULL,
    project_id UUID,
    recognition_date DATE NOT NULL,
    category_snapshot JSONB NOT NULL,
    project_snapshot JSONB,
    net_amount NUMERIC(38, 18) NOT NULL,
    tax_amount NUMERIC(38, 18) NOT NULL,
    total_amount NUMERIC(38, 18) NOT NULL,
    id UUID NOT NULL,
    ledger_id UUID NOT NULL,
    version INTEGER DEFAULT '1' NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    PRIMARY KEY (id),
    CONSTRAINT uq_document_revision_line_id_ledger UNIQUE (id, ledger_id),
    CONSTRAINT fk_document_revision_line_ledger FOREIGN KEY(ledger_id) REFERENCES ledgers (id)
        ON DELETE RESTRICT,
    CONSTRAINT ck_document_revision_line_version CHECK (version = 1),
    CONSTRAINT uq_document_revision_line_source UNIQUE (revision_id, source_line_id),
    CONSTRAINT uq_document_revision_line_position UNIQUE (revision_id, line_no),
    CONSTRAINT fk_document_revision_line_header FOREIGN KEY(revision_id, confirmation_id,
        document_id, ledger_id, document_version) REFERENCES business_document_revisions (id,
        confirmation_id, document_id, ledger_id, document_version) ON DELETE RESTRICT,
    CONSTRAINT fk_document_revision_line_category FOREIGN KEY(category_id, ledger_id,
        category_kind) REFERENCES categories (id, ledger_id, kind) ON DELETE RESTRICT,
    CONSTRAINT fk_document_revision_line_project FOREIGN KEY(project_id, ledger_id) REFERENCES
        business_projects (id, ledger_id) ON DELETE RESTRICT,
    CONSTRAINT ck_document_revision_line_position CHECK (line_no BETWEEN 1 AND 200),
    CONSTRAINT ck_document_revision_line_description_date CHECK (description ~ '^[^[:space:]]'
        AND description ~ '[^[:space:]]$' AND recognition_date BETWEEN DATE '0001-01-01' AND
        DATE '9999-12-31'),
    CONSTRAINT ck_document_revision_line_snapshots CHECK (jsonb_typeof(category_snapshot) =
        'object' AND (category_snapshot->>'id' = category_id::text) IS TRUE AND ((project_id
        IS NULL AND project_snapshot IS NULL) OR (project_id IS NOT NULL AND project_snapshot
        IS NOT NULL AND jsonb_typeof(project_snapshot) = 'object' AND (project_snapshot->>'id'
        = project_id::text) IS TRUE)))
);
"""


_GUARD = r"""
CREATE FUNCTION coinpup_guard_document_revision_line() RETURNS trigger
LANGUAGE plpgsql SET search_path=pg_catalog AS $$
DECLARE header record; n numeric; t numeric;
    ratio_pattern constant text := '^(0|[1-9][0-9]{0,19})(\.[0-9]{1,18})?$';
    money_pattern constant text := '^-?(0|[1-9][0-9]{0,19})(\.[0-9]{1,18})?$';
BEGIN
    IF TG_OP <> 'INSERT' THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='Document revision lines are immutable',
            CONSTRAINT='ck_document_revision_line_immutable';
    END IF;
    PERFORM pg_advisory_xact_lock(18945999704708432);
    IF current_setting('transaction_isolation') <> 'read committed' THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='Invalid document revision isolation',
            CONSTRAINT='ck_document_revision_isolation';
    END IF;
    SELECT r.action, a.scale, d.document_kind INTO header
    FROM public.business_document_revisions r
    JOIN public.business_documents d ON (d.id,d.ledger_id)=(r.document_id,r.ledger_id)
    LEFT JOIN public.assets a ON a.asset_id=r.asset_id
    WHERE (r.id,r.document_id,r.ledger_id,r.confirmation_id,r.document_version)=
        (NEW.revision_id,NEW.document_id,NEW.ledger_id,NEW.confirmation_id,NEW.document_version);
    IF NOT FOUND THEN RETURN NEW; END IF; -- The immediate scoped header FK rejects this row.
    IF header.action <> 'correct' OR NEW.category_kind IS DISTINCT FROM
        (CASE header.document_kind WHEN 'invoice' THEN 'income' ELSE 'expense' END)
    THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='Invalid document replacement line',
            CONSTRAINT='ck_document_revision_line_replacement';
    END IF;
    IF NEW.quantity !~ ratio_pattern OR NEW.tax_rate_percent !~ ratio_pattern
        OR NEW.unit_price !~ money_pattern OR NEW.discount_amount !~ money_pattern
        OR length(split_part(NEW.unit_price,'.',2)) > header.scale
        OR length(split_part(NEW.discount_amount,'.',2)) > header.scale THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='Invalid exact pricing input',
            CONSTRAINT='ck_document_revision_line_pricing';
    END IF;
    n := NEW.quantity::numeric * NEW.unit_price::numeric - NEW.discount_amount::numeric;
    IF n >= 0 AND NEW.quantity::numeric > 0 AND NEW.unit_price::numeric >= 0
        AND NEW.discount_amount::numeric >= 0 THEN
        n := round(n,header.scale);
        t := round(n * NEW.tax_rate_percent::numeric * 0.01,header.scale);
        IF NEW.net_amount = n AND NEW.tax_amount = t AND NEW.total_amount = n+t THEN
            RETURN NEW;
        END IF;
    END IF;
    RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='Invalid exact document line price',
        CONSTRAINT='ck_document_revision_line_pricing';
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
    _change_types(_OLD_ENTITY_TYPES + _TABLES)
    op.execute(_GUARD)
    op.execute(
        "CREATE TRIGGER tr_document_revision_lines_guard BEFORE INSERT OR UPDATE OR DELETE "
        "ON business_document_revision_lines FOR EACH ROW "
        "EXECUTE FUNCTION coinpup_guard_document_revision_line()"
    )
    op.execute(_CHANGE_TRIGGER_SQL["business_document_revision_lines"])


def downgrade():
    op.execute("SELECT pg_advisory_xact_lock(18945999704708432)")
    op.execute("LOCK TABLE public.business_document_revision_lines IN ACCESS EXCLUSIVE MODE")
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM public.business_document_revision_lines)
                OR EXISTS (SELECT 1 FROM public.change_log
                    WHERE entity_type='business_document_revision_lines') THEN
                RAISE EXCEPTION USING ERRCODE='23514',
                    MESSAGE='Document revision line history prevents downgrade',
                    CONSTRAINT='ck_document_revision_line_downgrade';
            END IF;
        END; $$;
    """)
    op.drop_table("business_document_revision_lines")
    op.execute("DROP FUNCTION coinpup_guard_document_revision_line()")
    _change_types(_OLD_ENTITY_TYPES)
