"""Delivery configuration with stable captured plans and immutable revisions."""

from alembic import op

revision = "20261011_0026"
down_revision = "20261010_0025"
branch_labels = None
depends_on = None

_TABLES = ("reminder_delivery_configs", "reminder_delivery_revisions")
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
    "'reminder_events', "
    "'reminder_event_revisions', "
).removesuffix(", ")
_ENTITY_TYPES = _OLD_ENTITY_TYPES + ", 'reminder_delivery_configs', 'reminder_delivery_revisions'"
_CHANGE_TRIGGER_SQL = {
    table: f"CREATE TRIGGER trg_{table}_change_log AFTER INSERT OR UPDATE ON {table} "
    "FOR EACH ROW EXECUTE FUNCTION coinpup_record_change()"
    for table in _TABLES
}

_SCHEMA_SQL = """
CREATE TABLE reminder_delivery_configs (
        id UUID NOT NULL,
        ledger_id UUID NOT NULL,
        event_id UUID NOT NULL,
        version INTEGER DEFAULT '1' NOT NULL,
        enabled BOOLEAN DEFAULT false NOT NULL,
        lead_days INTEGER NOT NULL,
        timezone VARCHAR(64) NOT NULL,
        minute INTEGER NOT NULL,
        locale VARCHAR(2) NOT NULL,
        inbox_enabled BOOLEAN NOT NULL,
        email_enabled BOOLEAN NOT NULL,
        email_recipient VARCHAR(320),
        planned_for_date DATE,
        planned_at TIMESTAMP WITH TIME ZONE,
        planned_timezone VARCHAR(64),
        planned_minute INTEGER,
        planned_lead_days INTEGER,
        resolved_minute INTEGER,
        planned_tzdata_version VARCHAR(32),
        blocked_reason VARCHAR(32),
        last_actor_id UUID NOT NULL,
        last_action VARCHAR(16) NOT NULL,
        created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
        updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
        PRIMARY KEY (id),
        CONSTRAINT uq_reminder_delivery_configs_id_ledger UNIQUE (id, ledger_id),
        CONSTRAINT uq_reminder_delivery_config_event UNIQUE (event_id),
        CONSTRAINT fk_reminder_delivery_config_event FOREIGN KEY(event_id, ledger_id) REFERENCES
    reminder_events (id, ledger_id) ON DELETE RESTRICT,
        CONSTRAINT fk_reminder_delivery_config_actor FOREIGN KEY(ledger_id, last_actor_id)
    REFERENCES ledgers (id, owner_id) ON DELETE RESTRICT,
        CONSTRAINT ck_reminder_delivery_config_version CHECK (version > 0),
        CONSTRAINT ck_reminder_delivery_config_lead CHECK (lead_days BETWEEN 0 AND 3660),
        CONSTRAINT ck_reminder_delivery_config_minute CHECK (minute BETWEEN 0 AND 1439),
        CONSTRAINT ck_reminder_delivery_config_locale CHECK (locale IN ('zh','en')),
        CONSTRAINT ck_reminder_delivery_config_channel CHECK (NOT enabled OR inbox_enabled OR
    email_enabled),
        CONSTRAINT ck_reminder_delivery_config_recipient CHECK ((NOT email_enabled OR
    email_recipient IS NOT NULL) AND (email_recipient IS NULL OR (btrim(email_recipient) <> ''
    AND email_recipient !~ '[[:cntrl:]]'))),
        CONSTRAINT ck_reminder_delivery_config_timezone CHECK (timezone ~
    '^[A-Za-z0-9_+-]+(/[A-Za-z0-9_+-]+)*$' AND timezone NOT IN ('localtime','posixrules') AND
    timezone NOT LIKE 'posix/%%' AND timezone NOT LIKE 'right/%%'),
        CONSTRAINT ck_reminder_delivery_config_plan_complete CHECK ((planned_for_date IS NULL
    AND planned_at IS NULL AND planned_timezone IS NULL AND planned_minute IS NULL AND
    planned_lead_days IS NULL AND resolved_minute IS NULL AND planned_tzdata_version IS NULL) OR
    (planned_for_date IS NOT NULL AND planned_at IS NOT NULL AND planned_timezone IS NOT NULL
    AND planned_minute IS NOT NULL AND planned_lead_days IS NOT NULL AND resolved_minute IS NOT
    NULL AND planned_tzdata_version IS NOT NULL)),
        CONSTRAINT ck_reminder_delivery_config_plan_range CHECK (planned_for_date IS NULL OR
    (planned_for_date BETWEEN DATE '0001-01-01' AND DATE '9999-12-31' AND isfinite(planned_at)
    AND EXTRACT(YEAR FROM planned_at AT TIME ZONE 'UTC') BETWEEN 1 AND 9999 AND
    planned_lead_days BETWEEN 0 AND 3660 AND planned_minute BETWEEN 0 AND 1439 AND
    resolved_minute BETWEEN planned_minute AND 1439 AND planned_timezone ~
    '^[A-Za-z0-9_+-]+(/[A-Za-z0-9_+-]+)*$' AND planned_timezone NOT IN
    ('localtime','posixrules') AND planned_timezone NOT LIKE 'posix/%%' AND planned_timezone NOT
    LIKE 'right/%%' AND btrim(planned_tzdata_version) <> '')),
        CONSTRAINT ck_reminder_delivery_config_enabled CHECK (enabled = (blocked_reason IS
    DISTINCT FROM 'disabled')),
        CONSTRAINT ck_reminder_delivery_config_ready CHECK (blocked_reason IS NOT NULL OR
    (enabled AND planned_at IS NOT NULL AND timezone = planned_timezone AND minute =
    planned_minute AND lead_days = planned_lead_days)),
        CONSTRAINT ck_reminder_delivery_config_blocked CHECK (blocked_reason IS NULL OR
    blocked_reason IN ('disabled','unplanned','missing_date','completed','archived_event','archi
    ved_entity','timezone_unavailable','date_out_of_range','local_date_unavailable')),
        CONSTRAINT ck_reminder_delivery_config_action CHECK (last_action IN
    ('create','configure','plan'))
);
CREATE INDEX ix_reminder_delivery_configs_ledger_id ON reminder_delivery_configs (ledger_id);
CREATE TABLE reminder_delivery_revisions (
        id UUID DEFAULT gen_random_uuid() NOT NULL,
        ledger_id UUID NOT NULL,
        config_id UUID NOT NULL,
        version INTEGER NOT NULL,
        snapshot JSONB NOT NULL,
        created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
        PRIMARY KEY (id),
        CONSTRAINT uq_reminder_delivery_revisions_id_ledger UNIQUE (id, ledger_id),
        CONSTRAINT uq_reminder_delivery_config_revision_version UNIQUE (config_id, version),
        CONSTRAINT fk_reminder_delivery_config_revision FOREIGN KEY(config_id, ledger_id)
    REFERENCES reminder_delivery_configs (id, ledger_id) ON DELETE RESTRICT,
        CONSTRAINT ck_reminder_delivery_config_revision_version CHECK (version > 0),
        CONSTRAINT ck_reminder_delivery_config_revision_snapshot CHECK (jsonb_typeof(snapshot) =
    'object')
);
CREATE INDEX ix_reminder_delivery_revisions_ledger_id ON reminder_delivery_revisions
    (ledger_id);
"""

_HISTORY_SQL = """
CREATE FUNCTION coinpup_guard_delivery_config() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog AS $$
DECLARE allowed text[] := ARRAY['version','updated_at','last_actor_id','last_action'];
        clock_fields text[] := ARRAY['planned_for_date','planned_at','planned_timezone',
            'planned_minute','planned_lead_days','resolved_minute','planned_tzdata_version'];
BEGIN
    PERFORM pg_advisory_xact_lock(18945999704708432);
    IF TG_OP = 'INSERT' THEN
        IF NEW.version = 1 AND NEW.last_action = 'create' THEN RETURN NEW; END IF;
    ELSIF TG_OP = 'UPDATE' AND NEW.version::bigint = OLD.version::bigint + 1 THEN
        CASE NEW.last_action
            WHEN 'configure' THEN allowed := allowed || ARRAY['enabled','lead_days','timezone',
                'minute','locale','inbox_enabled','email_enabled','email_recipient','blocked_reason'];
            WHEN 'plan' THEN allowed := allowed || clock_fields || ARRAY['blocked_reason'];
            ELSE allowed := ARRAY[]::text[];
        END CASE;
        IF OLD.planned_at IS NOT NULL THEN
            IF NEW.planned_at IS NULL OR (
                ROW(NEW.planned_for_date, NEW.planned_timezone, NEW.planned_minute,
                    NEW.planned_lead_days) IS NOT DISTINCT FROM
                ROW(OLD.planned_for_date, OLD.planned_timezone, OLD.planned_minute,
                    OLD.planned_lead_days)
                AND ROW(NEW.planned_at, NEW.resolved_minute, NEW.planned_tzdata_version)
                    IS DISTINCT FROM
                    ROW(OLD.planned_at, OLD.resolved_minute, OLD.planned_tzdata_version)
            ) THEN
                RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'Saved plan is stable',
                    CONSTRAINT = 'ck_reminder_delivery_plan_stable';
            END IF;
        END IF;
        IF (to_jsonb(NEW) - allowed) IS NOT DISTINCT FROM (to_jsonb(OLD) - allowed)
            THEN RETURN NEW; END IF;
    END IF;
    RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'Invalid delivery configuration transition',
        CONSTRAINT = 'ck_reminder_delivery_config_transition';
END; $$;

CREATE FUNCTION coinpup_guard_delivery_revision() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog AS $$
DECLARE current_state jsonb;
BEGIN
    PERFORM pg_advisory_xact_lock(18945999704708432);
    IF TG_OP = 'INSERT' THEN
        SELECT to_jsonb(c) INTO current_state FROM public.reminder_delivery_configs c
        WHERE c.id = NEW.config_id AND c.ledger_id = NEW.ledger_id AND c.version = NEW.version;
        IF current_state IS NOT NULL AND NEW.snapshot = current_state THEN RETURN NEW; END IF;
    END IF;
    RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'Delivery revisions are immutable',
        CONSTRAINT = 'ck_reminder_delivery_revision_history';
END; $$;

CREATE FUNCTION coinpup_capture_delivery_revision() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog AS $$
BEGIN
    INSERT INTO public.reminder_delivery_revisions(config_id, ledger_id, version, snapshot)
    VALUES (NEW.id, NEW.ledger_id, NEW.version, to_jsonb(NEW));
    RETURN NULL;
END; $$;

CREATE TRIGGER tr_delivery_config_guard BEFORE INSERT OR UPDATE OR DELETE
ON reminder_delivery_configs
FOR EACH ROW EXECUTE FUNCTION coinpup_guard_delivery_config();
CREATE TRIGGER tr_delivery_revision_guard BEFORE INSERT OR UPDATE OR DELETE
ON reminder_delivery_revisions
FOR EACH ROW EXECUTE FUNCTION coinpup_guard_delivery_revision();
CREATE TRIGGER tr_delivery_revision_capture AFTER INSERT OR UPDATE ON reminder_delivery_configs
FOR EACH ROW EXECUTE FUNCTION coinpup_capture_delivery_revision();

"""


def upgrade():
    op.execute("SELECT pg_advisory_xact_lock(18945999704708432)")
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
    op.execute(
        "LOCK TABLE reminder_delivery_configs, reminder_delivery_revisions IN ACCESS EXCLUSIVE MODE"
    )
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM public.reminder_delivery_configs)
               OR EXISTS (SELECT 1 FROM public.reminder_delivery_revisions)
               OR EXISTS (SELECT 1 FROM public.change_log WHERE entity_type IN
                   ('reminder_delivery_configs','reminder_delivery_revisions')) THEN
                RAISE EXCEPTION USING ERRCODE = '23514',
                    MESSAGE = 'Delivery configuration history prevents downgrade',
                    CONSTRAINT = 'ck_reminder_delivery_downgrade';
            END IF;
        END; $$
    """)
    for table in reversed(_TABLES):
        op.drop_table(table)
    op.execute("DROP FUNCTION coinpup_capture_delivery_revision()")
    op.execute("DROP FUNCTION coinpup_guard_delivery_revision()")
    op.execute("DROP FUNCTION coinpup_guard_delivery_config()")
    op.drop_constraint("ck_change_log_entity_type", "change_log", type_="check")
    op.create_check_constraint(
        "ck_change_log_entity_type", "change_log", f"entity_type IN ({_OLD_ENTITY_TYPES})"
    )
