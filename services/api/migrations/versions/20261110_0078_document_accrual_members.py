"""Bind confirmed accrual operations to their actual former and replacement line facts."""

from alembic import op

revision = "20261110_0078"
down_revision = "20261110_0077"
branch_labels = None
depends_on = None


_TABLES = ("business_document_accruals",)
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
)
_ENTITY_TYPES = ", ".join(repr(value) for value in _OLD_ENTITY_TYPES + _TABLES)
_CHANGE_TRIGGER_SQL = {
    table: f"CREATE TRIGGER trg_{table}_change_log AFTER INSERT OR UPDATE ON {table} "
    "FOR EACH ROW EXECUTE FUNCTION coinpup_record_change()"
    for table in _TABLES
}

_SCHEMA_SQL = r"""
CREATE TABLE business_document_accruals (
    revision_id UUID NOT NULL,
    confirmation_id UUID NOT NULL,
    document_id UUID NOT NULL,
    document_version INTEGER NOT NULL,
    source_line_id UUID NOT NULL,
    replacement_fact_id UUID,
    action VARCHAR(7) NOT NULL,
    operation_id UUID NOT NULL,
    operation_version INTEGER NOT NULL,
    previous_journal_id UUID,
    reversal_journal_id UUID,
    journal_id UUID,
    id UUID NOT NULL,
    ledger_id UUID NOT NULL,
    version INTEGER DEFAULT '1' NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    PRIMARY KEY (id),
    CONSTRAINT uq_document_accrual_id_ledger UNIQUE (id, ledger_id),
    CONSTRAINT fk_document_accrual_ledger FOREIGN KEY(ledger_id) REFERENCES ledgers (id) ON
        DELETE RESTRICT,
    CONSTRAINT ck_document_accrual_version CHECK (version = 1),
    CONSTRAINT uq_document_accrual_source UNIQUE (revision_id, source_line_id),
    CONSTRAINT uq_document_accrual_operation UNIQUE (operation_id, operation_version),
    CONSTRAINT uq_document_accrual_fact UNIQUE (replacement_fact_id),
    CONSTRAINT uq_document_accrual_journal UNIQUE (journal_id),
    CONSTRAINT uq_document_accrual_reversal UNIQUE (reversal_journal_id),
    CONSTRAINT fk_document_accrual_group FOREIGN KEY(revision_id, confirmation_id,
        document_id, ledger_id, document_version) REFERENCES business_document_revisions (id,
        confirmation_id, document_id, ledger_id, document_version) ON DELETE RESTRICT,
    CONSTRAINT fk_document_accrual_source FOREIGN KEY(source_line_id, document_id, ledger_id)
        REFERENCES business_document_lines (id, document_id, ledger_id) ON DELETE RESTRICT,
    CONSTRAINT fk_document_accrual_fact FOREIGN KEY(replacement_fact_id, ledger_id) REFERENCES
        business_document_revision_lines (id, ledger_id) ON DELETE RESTRICT,
    CONSTRAINT fk_document_accrual_operation FOREIGN KEY(operation_id, ledger_id) REFERENCES
        financial_operations (id, ledger_id) ON DELETE RESTRICT,
    CONSTRAINT fk_document_accrual_previous FOREIGN KEY(previous_journal_id, operation_id,
        ledger_id) REFERENCES journals (id, operation_id, ledger_id) DEFERRABLE INITIALLY
        DEFERRED,
    CONSTRAINT fk_document_accrual_reversal FOREIGN KEY(reversal_journal_id, operation_id,
        ledger_id) REFERENCES journals (id, operation_id, ledger_id) DEFERRABLE INITIALLY
        DEFERRED,
    CONSTRAINT fk_document_accrual_journal FOREIGN KEY(journal_id, operation_id, ledger_id)
        REFERENCES journals (id, operation_id, ledger_id) DEFERRABLE INITIALLY DEFERRED,
    CONSTRAINT ck_document_accrual_action CHECK (action IN ('create','correct','cancel')),
    CONSTRAINT ck_document_accrual_previous CHECK ((action = 'create' AND operation_version =
        1 AND previous_journal_id IS NULL AND reversal_journal_id IS NULL) OR (action IN
        ('correct','cancel') AND operation_version > 1 AND previous_journal_id IS NOT NULL AND
        reversal_journal_id IS NOT NULL AND previous_journal_id <> reversal_journal_id)),
    CONSTRAINT ck_document_accrual_next CHECK ((action = 'cancel' AND replacement_fact_id IS
        NULL AND journal_id IS NULL) OR (action IN ('create','correct') AND
        replacement_fact_id IS NOT NULL AND journal_id IS NOT NULL AND journal_id IS DISTINCT
        FROM previous_journal_id AND journal_id IS DISTINCT FROM reversal_journal_id))
);
"""


_GUARD = """
CREATE FUNCTION coinpup_guard_document_accrual() RETURNS trigger
LANGUAGE plpgsql SET search_path=pg_catalog AS $$
DECLARE h public.business_document_revisions%ROWTYPE; previous_op uuid; previous_journal uuid;
    previous_version integer;
BEGIN
    IF TG_OP <> 'INSERT' THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='Document accrual history is immutable',
            CONSTRAINT='ck_document_accrual_immutable';
    END IF;
    PERFORM pg_advisory_xact_lock(18945999704708432);
    IF current_setting('transaction_isolation') <> 'read committed' THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='Invalid document revision isolation',
            CONSTRAINT='ck_document_revision_isolation';
    END IF;
    SELECT * INTO h FROM public.business_document_revisions
    WHERE (id,confirmation_id,document_id,ledger_id,document_version)=
          (NEW.revision_id,NEW.confirmation_id,NEW.document_id,NEW.ledger_id,NEW.document_version);
    IF NOT FOUND THEN RETURN NEW; END IF;
    IF NEW.replacement_fact_id IS NOT NULL AND (h.action <> 'correct' OR NOT EXISTS (
        SELECT 1 FROM public.business_document_revision_lines f
        WHERE f.id=NEW.replacement_fact_id AND (f.revision_id,f.source_line_id,f.ledger_id)=
            (NEW.revision_id,NEW.source_line_id,NEW.ledger_id) AND f.total_amount>0
    )) THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='Invalid replacement accrual source',
            CONSTRAINT='ck_document_accrual_replacement_source';
    END IF;
    IF h.previous_revision_id IS NULL THEN
        SELECT p.operation_id,p.journal_id,1 INTO previous_op,previous_journal,previous_version
        FROM public.business_postings p WHERE (p.confirmation_id,p.line_id,p.ledger_id)=
            (h.confirmation_id,NEW.source_line_id,h.ledger_id);
    ELSE
        SELECT a.operation_id,a.journal_id,a.operation_version
            INTO previous_op,previous_journal,previous_version
        FROM public.business_document_accruals a
        WHERE (a.revision_id,a.source_line_id,a.ledger_id)=
            (h.previous_revision_id,NEW.source_line_id,h.ledger_id) AND a.journal_id IS NOT NULL;
    END IF;
    IF (NEW.action='create' AND (previous_op IS NOT NULL OR EXISTS (
            SELECT 1 FROM public.business_postings p WHERE p.operation_id=NEW.operation_id)))
       OR (NEW.action IN ('correct','cancel') AND (previous_op IS NULL OR
           (NEW.operation_id,NEW.previous_journal_id,NEW.operation_version::bigint) IS DISTINCT FROM
           (previous_op,previous_journal,previous_version::bigint+1))) THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='Invalid previous accrual source',
            CONSTRAINT='ck_document_accrual_previous_source';
    END IF;
    RETURN NEW;
END; $$;

CREATE FUNCTION coinpup_check_document_accrual_journals() RETURNS trigger
LANGUAGE plpgsql SET search_path=pg_catalog AS $$
DECLARE h public.business_document_revisions%ROWTYPE; p public.journals%ROWTYPE;
    r public.journals%ROWTYPE; j public.journals%ROWTYPE;
    f public.business_document_revision_lines%ROWTYPE;
BEGIN
    SELECT * INTO h FROM public.business_document_revisions WHERE id=NEW.revision_id;
    IF NOT EXISTS (SELECT 1 FROM public.financial_operations o JOIN public.business_documents d
        ON d.ledger_id=o.ledger_id WHERE o.id=NEW.operation_id AND d.id=NEW.document_id
        AND o.ledger_id=NEW.ledger_id AND o.kind=d.document_kind || '_accrual') THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='Invalid accrual operation kind',
            CONSTRAINT='ck_document_accrual_journals';
    END IF;
    IF NEW.previous_journal_id IS NOT NULL THEN
        SELECT * INTO p FROM public.journals WHERE id=NEW.previous_journal_id;
        SELECT * INTO r FROM public.journals WHERE id=NEW.reversal_journal_id;
        IF (p.operation_id,p.ledger_id,p.journal_kind,p.operation_version::bigint+1,
            r.operation_id,r.ledger_id,r.journal_kind,r.operation_version,r.reverses_journal_id,
            r.transaction_date,r.recognition_date,r.description,r.revision_reason)
            IS DISTINCT FROM (NEW.operation_id,NEW.ledger_id,'posting'::varchar,
            NEW.operation_version::bigint,NEW.operation_id,NEW.ledger_id,'reversal'::varchar,
            NEW.operation_version,NEW.previous_journal_id,p.transaction_date,p.recognition_date,
            p.description,h.reason) THEN
            RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='Invalid accrual reversal journal',
                CONSTRAINT='ck_document_accrual_journals';
        END IF;
    END IF;
    IF NEW.journal_id IS NOT NULL THEN
        SELECT * INTO j FROM public.journals WHERE id=NEW.journal_id;
        SELECT * INTO f FROM public.business_document_revision_lines
        WHERE id=NEW.replacement_fact_id;
        IF (j.operation_id,j.ledger_id,j.journal_kind,j.operation_version,j.transaction_date,
            j.recognition_date,j.description,j.revision_reason) IS DISTINCT FROM
            (NEW.operation_id,NEW.ledger_id,'posting'::varchar,NEW.operation_version,h.issue_date,
            f.recognition_date,f.description,(CASE WHEN NEW.operation_version>1 THEN h.reason END))
        THEN
            RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='Invalid replacement accrual journal',
                CONSTRAINT='ck_document_accrual_journals';
        END IF;
    END IF;
    RETURN NULL;
END; $$;
"""


def _change_types(types):
    op.drop_constraint("ck_change_log_entity_type", "change_log", type_="check")
    op.create_check_constraint(
        "ck_change_log_entity_type",
        "change_log",
        "entity_type IN (" + ", ".join(repr(t) for t in types) + ")",
    )


def upgrade():
    op.execute("SELECT pg_advisory_xact_lock(18945999704708432)")
    op.execute(_SCHEMA_SQL)
    _change_types(_OLD_ENTITY_TYPES + _TABLES)
    op.execute(_GUARD)
    op.execute(
        "CREATE TRIGGER tr_document_accrual_guard BEFORE INSERT OR UPDATE OR DELETE "
        "ON business_document_accruals FOR EACH ROW "
        "EXECUTE FUNCTION coinpup_guard_document_accrual()"
    )
    op.execute(
        "CREATE CONSTRAINT TRIGGER tr_document_accrual_journals AFTER INSERT "
        "ON business_document_accruals DEFERRABLE INITIALLY DEFERRED FOR EACH ROW "
        "EXECUTE FUNCTION coinpup_check_document_accrual_journals()"
    )
    op.execute(_CHANGE_TRIGGER_SQL["business_document_accruals"])


def downgrade():
    op.execute("SELECT pg_advisory_xact_lock(18945999704708432)")
    op.execute("LOCK TABLE public.business_document_accruals IN ACCESS EXCLUSIVE MODE")
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM public.business_document_accruals)
                OR EXISTS (SELECT 1 FROM public.change_log
                    WHERE entity_type='business_document_accruals') THEN
                RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='Accrual history prevents downgrade',
                    CONSTRAINT='ck_document_accrual_downgrade';
            END IF;
        END; $$;
    """)
    op.drop_table("business_document_accruals")
    op.execute("DROP FUNCTION coinpup_guard_document_accrual()")
    op.execute("DROP FUNCTION coinpup_check_document_accrual_journals()")
    _change_types(_OLD_ENTITY_TYPES)
