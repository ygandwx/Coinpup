"""Scoped reminder state with atomic immutable revision snapshots."""

from alembic import op

revision = "20261010_0025"
down_revision = "20261009_0024"
branch_labels = None
depends_on = None

_TABLES = ("reminder_events", "reminder_event_revisions")
_OLD_ENTITY_TYPES = (
    "'entities', "
    "'ledgers', "
    "'accounts', "
    "'account_assets', "
    "'categories', "
    "'assets', "
    "'financial_operations', "
    "'stored_files', "
    "'operation_file_links', "
    "'ocr_jobs', "
    "'ocr_drafts', "
    "'ocr_confirmations', "
    "'business_parties', "
    "'business_documents', "
    "'business_document_lines', "
    "'ledger_periods', "
    "'ledger_period_audits', "
    "'ledger_period_receipts', "
    "'business_projects', "
    "'recurring_invoice_rules', "
    "'recurring_invoice_instances', "
).removesuffix(", ")
_ENTITY_TYPES = _OLD_ENTITY_TYPES + ", 'reminder_events', 'reminder_event_revisions'"
_CHANGE_TRIGGER_SQL = {
    table: f"CREATE TRIGGER trg_{table}_change_log AFTER INSERT OR UPDATE ON {table} "
    "FOR EACH ROW EXECUTE FUNCTION coinpup_record_change()"
    for table in _TABLES
}

_EVALUATION_SQL = """
CREATE FUNCTION coinpup_valid_reminder_evaluation(v jsonb, state text, calculated date)
RETURNS boolean LANGUAGE plpgsql IMMUTABLE SET search_path = pg_catalog AS $$
BEGIN
    IF v IS NULL THEN RETURN state = 'missing_parameters' AND calculated IS NULL; END IF;
    IF jsonb_typeof(v) IS DISTINCT FROM 'object'
       OR NOT (v ?& ARRAY['rule','inputs','status','reasons','missing','calculated_date'])
       OR v->>'status' IS DISTINCT FROM state
       OR jsonb_typeof(v->'rule') IS DISTINCT FROM 'object'
       OR jsonb_typeof(v->'inputs') IS DISTINCT FROM 'object'
       OR jsonb_typeof(v->'reasons') IS DISTINCT FROM 'array'
       OR jsonb_typeof(v->'missing') IS DISTINCT FROM 'array'
       OR jsonb_typeof(v->'rule'->'sources') IS DISTINCT FROM 'array' THEN RETURN false; END IF;
    IF NOT ((v->'rule') ?& ARRAY['id','version','checked_on','sources'])
       OR jsonb_typeof(v->'rule'->'id') IS DISTINCT FROM 'string'
       OR jsonb_typeof(v->'rule'->'version') IS DISTINCT FROM 'string'
       OR jsonb_typeof(v->'rule'->'checked_on') IS DISTINCT FROM 'string'
       OR btrim(v->'rule'->>'id') = '' OR btrim(v->'rule'->>'version') = ''
       OR (v->'rule'->>'checked_on') !~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}$'
       OR jsonb_array_length(v->'rule'->'sources') = 0 THEN RETURN false; END IF;
    IF EXISTS (SELECT 1 FROM jsonb_array_elements(
        (v->'reasons') || (v->'missing') || (v->'rule'->'sources')) item
        WHERE jsonb_typeof(item) <> 'string' OR btrim(item #>> '{}') = '') THEN RETURN false; END
        IF;
    IF calculated IS NOT NULL AND calculated NOT BETWEEN DATE '0001-01-01' AND DATE '9999-12-31'
       THEN RETURN false; END IF;
    PERFORM (v->'rule'->>'checked_on')::date;
    IF state = 'calculated' AND calculated IS NULL THEN RETURN false; END IF;
    IF state IN ('missing_parameters','not_applicable') AND calculated IS NOT NULL THEN RETURN
        false; END IF;
    RETURN (v->'calculated_date') IS NOT DISTINCT FROM
        CASE WHEN calculated IS NULL THEN 'null'::jsonb
             ELSE to_jsonb(to_char(calculated, 'YYYY-MM-DD')) END;
EXCEPTION WHEN invalid_datetime_format OR datetime_field_overflow THEN RETURN false;
END; $$;
"""

_SCHEMA_SQL = """
CREATE TABLE reminder_events (
	id UUID NOT NULL,
	ledger_id UUID NOT NULL,
	version INTEGER DEFAULT '1' NOT NULL,
	event_kind VARCHAR(16) NOT NULL,
	title VARCHAR(160) NOT NULL,
	notes VARCHAR(2000),
	evaluation JSONB,
	evaluation_status VARCHAR(24) NOT NULL,
	calculated_date DATE,
	manual_due_date DATE,
	manual_reason VARCHAR(500),
	completed BOOLEAN DEFAULT false NOT NULL,
	archived BOOLEAN DEFAULT false NOT NULL,
	last_actor_id UUID NOT NULL,
	last_action VARCHAR(16) NOT NULL,
	last_reason VARCHAR(500),
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT uq_reminder_events_id_ledger UNIQUE (id, ledger_id),
	CONSTRAINT fk_reminder_event_actor FOREIGN KEY(ledger_id, last_actor_id) REFERENCES ledgers (id,
        owner_id) ON DELETE RESTRICT,
	CONSTRAINT ck_reminder_event_version CHECK (version > 0),
	CONSTRAINT ck_reminder_event_kind CHECK (event_kind IN ('annual', 'tax', 'certificate')),
	CONSTRAINT ck_reminder_event_title CHECK (btrim(title) <> ''),
	CONSTRAINT ck_reminder_event_status CHECK (evaluation_status IN ('calculated',
        'missing_parameters', 'needs_verification', 'not_applicable')),
	CONSTRAINT ck_reminder_event_evaluation CHECK (coinpup_valid_reminder_evaluation(evaluation,
        evaluation_status, calculated_date)),
	CONSTRAINT ck_reminder_event_manual CHECK ((manual_due_date IS NULL AND manual_reason IS NULL)
        OR (manual_due_date IS NOT NULL AND manual_due_date BETWEEN DATE '0001-01-01' AND DATE
        '9999-12-31' AND manual_reason IS NOT NULL AND btrim(manual_reason) <> '')),
	CONSTRAINT ck_reminder_event_action CHECK (last_action IN ('create', 'edit', 'recalculate',
        'set_manual', 'clear_manual', 'complete', 'reopen', 'archive', 'restore'))
);
CREATE INDEX ix_reminder_events_ledger_id ON reminder_events (ledger_id);
CREATE TABLE reminder_event_revisions (
	id UUID DEFAULT gen_random_uuid() NOT NULL,
	ledger_id UUID NOT NULL,
	event_id UUID NOT NULL,
	version INTEGER NOT NULL,
	snapshot JSONB NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT uq_reminder_event_revisions_id_ledger UNIQUE (id, ledger_id),
	CONSTRAINT uq_reminder_event_revision_version UNIQUE (event_id, version),
	CONSTRAINT fk_reminder_revision_event FOREIGN KEY(event_id, ledger_id) REFERENCES
        reminder_events (id, ledger_id) ON DELETE RESTRICT,
	CONSTRAINT ck_reminder_revision_version CHECK (version > 0),
	CONSTRAINT ck_reminder_revision_snapshot CHECK (jsonb_typeof(snapshot) = 'object')
);
CREATE INDEX ix_reminder_event_revisions_ledger_id ON reminder_event_revisions (ledger_id);
"""

_HISTORY_SQL = """
CREATE FUNCTION coinpup_guard_reminder_event() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog AS $$
DECLARE allowed text[] :=
        ARRAY['version','updated_at','last_actor_id','last_action','last_reason'];
        valid boolean := true;
BEGIN
    PERFORM pg_advisory_xact_lock(18945999704708432);
    IF TG_OP = 'INSERT' THEN
        IF NEW.version = 1 AND NEW.last_action = 'create' AND NOT NEW.completed AND NOT
        NEW.archived
           THEN RETURN NEW; END IF;
    ELSIF TG_OP = 'UPDATE' AND NEW.version::bigint = OLD.version::bigint + 1 THEN
        CASE NEW.last_action
            WHEN 'edit' THEN allowed := allowed || ARRAY['title','notes'];
            WHEN 'recalculate' THEN allowed := allowed ||
        ARRAY['evaluation','evaluation_status','calculated_date'];
            WHEN 'set_manual' THEN
                IF NEW.manual_due_date IS NULL OR NEW.manual_reason IS NULL
                   OR NEW.last_reason IS DISTINCT FROM NEW.manual_reason THEN
                    RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE =
        'Manual date needs a reason',
                        CONSTRAINT = 'ck_reminder_event_transition';
                END IF;
                allowed := allowed || ARRAY['manual_due_date','manual_reason'];
            WHEN 'clear_manual' THEN
                IF OLD.manual_due_date IS NULL OR NEW.manual_due_date IS NOT NULL
                   OR NEW.manual_reason IS NOT NULL OR NEW.last_reason IS NULL
                   OR btrim(NEW.last_reason) = '' THEN
                    RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE =
        'Manual date needs a reason',
                        CONSTRAINT = 'ck_reminder_event_transition';
                END IF;
                allowed := allowed || ARRAY['manual_due_date','manual_reason'];
            WHEN 'complete' THEN
                valid := NOT OLD.completed AND NEW.completed;
                allowed := allowed || ARRAY['completed'];
            WHEN 'reopen' THEN
                valid := OLD.completed AND NOT NEW.completed;
                allowed := allowed || ARRAY['completed'];
            WHEN 'archive' THEN
                valid := NOT OLD.archived AND NEW.archived;
                allowed := allowed || ARRAY['archived'];
            WHEN 'restore' THEN
                valid := OLD.archived AND NOT NEW.archived;
                allowed := allowed || ARRAY['archived'];
            ELSE allowed := ARRAY[]::text[];
        END CASE;
        IF valid AND (to_jsonb(NEW) - allowed) IS NOT DISTINCT FROM (to_jsonb(OLD) - allowed)
           THEN RETURN NEW; END IF;
    END IF;
    RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'Invalid reminder transition',
        CONSTRAINT = 'ck_reminder_event_transition';
END; $$;

CREATE FUNCTION coinpup_guard_reminder_revision() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog AS $$
DECLARE current_state jsonb;
BEGIN
    PERFORM pg_advisory_xact_lock(18945999704708432);
    IF TG_OP = 'INSERT' THEN
        SELECT to_jsonb(e) INTO current_state FROM public.reminder_events e
        WHERE e.id = NEW.event_id AND e.ledger_id = NEW.ledger_id AND e.version = NEW.version;
        IF current_state IS NOT NULL AND NEW.snapshot = current_state THEN RETURN NEW; END IF;
    END IF;
    RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'Reminder revisions are immutable',
        CONSTRAINT = 'ck_reminder_revision_history';
END; $$;

CREATE FUNCTION coinpup_capture_reminder_revision() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog AS $$
BEGIN
    INSERT INTO public.reminder_event_revisions(event_id, ledger_id, version, snapshot)
    VALUES (NEW.id, NEW.ledger_id, NEW.version, to_jsonb(NEW));
    RETURN NULL;
END; $$;

CREATE TRIGGER tr_reminder_event_guard BEFORE INSERT OR UPDATE OR DELETE ON reminder_events
FOR EACH ROW EXECUTE FUNCTION coinpup_guard_reminder_event();
CREATE TRIGGER tr_reminder_revision_guard BEFORE INSERT OR UPDATE OR DELETE ON
        reminder_event_revisions
FOR EACH ROW EXECUTE FUNCTION coinpup_guard_reminder_revision();
CREATE TRIGGER tr_reminder_revision_capture AFTER INSERT OR UPDATE ON reminder_events
FOR EACH ROW EXECUTE FUNCTION coinpup_capture_reminder_revision();
"""


def upgrade():
    op.execute("SELECT pg_advisory_xact_lock(18945999704708432)")
    op.execute(_EVALUATION_SQL)
    op.execute(_SCHEMA_SQL)
    op.execute(_HISTORY_SQL)
    op.drop_constraint("ck_change_log_entity_type", "change_log", type_="check")
    op.create_check_constraint(
        "ck_change_log_entity_type", "change_log", f"entity_type IN ({_ENTITY_TYPES})"
    )
    for definition in _CHANGE_TRIGGER_SQL.values():
        op.execute(definition)


def downgrade():
    op.execute("SELECT pg_advisory_xact_lock(18945999704708432)")
    op.execute("LOCK TABLE reminder_events, reminder_event_revisions IN ACCESS EXCLUSIVE MODE")
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM public.reminder_events)
               OR EXISTS (SELECT 1 FROM public.reminder_event_revisions)
               OR EXISTS (SELECT 1 FROM public.change_log WHERE entity_type IN
                   ('reminder_events','reminder_event_revisions')) THEN
                RAISE EXCEPTION USING ERRCODE = '23514',
                    MESSAGE = 'Reminder history prevents downgrade',
                    CONSTRAINT = 'ck_reminder_downgrade';
            END IF;
        END; $$
    """)
    for table in reversed(_TABLES):
        op.drop_table(table)
    op.execute("DROP FUNCTION coinpup_capture_reminder_revision()")
    op.execute("DROP FUNCTION coinpup_guard_reminder_revision()")
    op.execute("DROP FUNCTION coinpup_guard_reminder_event()")
    op.execute("DROP FUNCTION coinpup_valid_reminder_evaluation(jsonb,text,date)")
    op.drop_constraint("ck_change_log_entity_type", "change_log", type_="check")
    op.create_check_constraint(
        "ck_change_log_entity_type", "change_log", f"entity_type IN ({_OLD_ENTITY_TYPES})"
    )
