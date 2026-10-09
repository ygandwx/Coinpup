"""Scoped recurring templates and immutable, contiguous draft instances."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "20261009_0024"
down_revision = "20261009_0023"
branch_labels = None
depends_on = None

_OLD_ENTITY_TYPES = (
    "'entities', 'ledgers', 'accounts', 'account_assets', 'categories', 'assets', "
    "'financial_operations', 'stored_files', 'operation_file_links', 'ocr_jobs', "
    "'ocr_drafts', 'ocr_confirmations', 'business_parties', 'business_documents', "
    "'business_document_lines', 'ledger_periods', 'ledger_period_audits', "
    "'ledger_period_receipts', 'business_projects'"
)
_ENTITY_TYPES = _OLD_ENTITY_TYPES + ", 'recurring_invoice_rules', 'recurring_invoice_instances'"
_TABLES = ("recurring_invoice_rules", "recurring_invoice_instances")
_CHANGE_TRIGGER_SQL = {
    table: f"CREATE TRIGGER trg_{table}_change_log AFTER INSERT OR UPDATE ON {table} "
    "FOR EACH ROW EXECUTE FUNCTION coinpup_record_change()"
    for table in _TABLES
}


CALENDAR_SQL = """
CREATE FUNCTION coinpup_recurring_date(a date, frequency text, gap integer, n integer)
RETURNS date LANGUAGE plpgsql IMMUTABLE SET search_path = pg_catalog AS $$
DECLARE steps bigint; result date;
BEGIN
    IF a IS NULL OR frequency IS NULL OR gap IS NULL OR n IS NULL
       OR a NOT BETWEEN DATE '0001-01-01' AND DATE '9999-12-31'
       OR gap NOT BETWEEN 1 AND 120 OR n < 0 THEN RETURN NULL; END IF;
    steps := gap::bigint * n;
    IF frequency IN ('day', 'week') THEN
        IF frequency = 'week' THEN steps := steps * 7; END IF;
        IF steps > 3652058 THEN RETURN NULL; END IF;
        result := a + steps::integer;
    ELSIF frequency IN ('month', 'year') THEN
        IF frequency = 'year' THEN steps := steps * 12; END IF;
        IF steps > 119987 THEN RETURN NULL; END IF;
        result := (a + make_interval(months => steps::integer))::date;
    ELSE RETURN NULL;
    END IF;
    IF result > DATE '9999-12-31' THEN RETURN NULL; END IF;
    RETURN result;
END; $$;
"""

INPUT_SQL = """
CREATE FUNCTION coinpup_draft_input(identifier uuid, ledger uuid)
RETURNS jsonb LANGUAGE sql STABLE SET search_path = pg_catalog AS $$
    SELECT jsonb_build_object(
        'id', d.id, 'document_kind', d.document_kind, 'party_id', d.party_id,
        'asset_id', d.asset_id, 'issue_date', d.issue_date, 'due_date', d.due_date,
        'notes', d.notes, 'lines', COALESCE((
            SELECT jsonb_agg(jsonb_build_object(
                'id', l.id, 'description', l.description, 'quantity', l.quantity,
                'unit_price', l.unit_price, 'discount_amount', l.discount_amount,
                'tax_rate_percent', l.tax_rate_percent, 'category_id', l.category_id,
                'project_id', l.project_id, 'recognition_date', l.recognition_date
            ) ORDER BY l.line_no)
            FROM public.business_document_lines l
            WHERE l.document_id = d.id AND l.ledger_id = d.ledger_id
                AND NOT l.archived AND l.quantity IS NOT NULL
        ), '[]'::jsonb)
    ) FROM public.business_documents d
    WHERE d.id = identifier AND d.ledger_id = ledger AND d.document_kind IS NOT NULL
$$;
"""

MATCH_SQL = """
CREATE FUNCTION coinpup_recurring_input_matches(source jsonb, generated jsonb, scheduled date)
RETURNS boolean LANGUAGE plpgsql IMMUTABLE SET search_path = pg_catalog AS $$
DECLARE offset_days integer; expected_due jsonb;
BEGIN
    offset_days := scheduled - (source->>'issue_date')::date;
    expected_due := CASE WHEN source->>'due_date' IS NULL THEN 'null'::jsonb
        ELSE to_jsonb((source->>'due_date')::date + offset_days) END;
    RETURN (generated - ARRAY['id', 'lines', 'issue_date', 'due_date'])
            IS NOT DISTINCT FROM (source - ARRAY['id', 'lines', 'issue_date', 'due_date'])
        AND generated->'issue_date' IS NOT DISTINCT FROM to_jsonb(scheduled)
        AND generated->'due_date' IS NOT DISTINCT FROM expected_due
        AND jsonb_array_length(generated->'lines') = jsonb_array_length(source->'lines')
        AND NOT EXISTS (
            SELECT 1 FROM jsonb_array_elements(source->'lines') WITH ORDINALITY a(line, n)
            JOIN jsonb_array_elements(generated->'lines') WITH ORDINALITY b(line, n)
                ON a.n = b.n
            WHERE (a.line - ARRAY['id', 'recognition_date'])
                IS DISTINCT FROM (b.line - ARRAY['id', 'recognition_date'])
                OR b.line->'recognition_date' IS DISTINCT FROM
                    to_jsonb((a.line->>'recognition_date')::date + offset_days)
        );
END; $$;
"""


RULE_SQL = """
CREATE FUNCTION coinpup_guard_recurring_rule() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog AS $$
DECLARE valid boolean := false; capture boolean := TG_OP = 'INSERT';
    source public.business_documents%ROWTYPE;
    fields text[] := ARRAY['version', 'archived', 'updated_at', 'name',
        'source_document_id', 'source_version', 'template_input', 'next_index'];
BEGIN
    PERFORM pg_advisory_xact_lock(18945999704708432);
    IF TG_OP = 'INSERT' THEN
        valid := NEW.version = 1 AND NOT NEW.archived AND NEW.next_index = 0;
        IF NOT EXISTS (SELECT 1 FROM pg_catalog.pg_timezone_names
                       WHERE name = NEW.timezone_name) THEN valid := false; END IF;
    ELSIF TG_OP = 'UPDATE' THEN
        valid := NEW.version::bigint = OLD.version::bigint + 1
            AND (to_jsonb(NEW) - fields) IS NOT DISTINCT FROM (to_jsonb(OLD) - fields);
        IF NEW.next_index IS DISTINCT FROM OLD.next_index THEN
            valid := valid AND NOT OLD.archived
                AND NEW.next_index::bigint = OLD.next_index::bigint + 1
                AND (to_jsonb(NEW) - ARRAY['version', 'updated_at', 'next_index'])
                    IS NOT DISTINCT FROM
                    (to_jsonb(OLD) - ARRAY['version', 'updated_at', 'next_index']);
        END IF;
        capture := NEW.source_document_id IS DISTINCT FROM OLD.source_document_id
            OR NEW.source_version IS DISTINCT FROM OLD.source_version
            OR NEW.template_input IS DISTINCT FROM OLD.template_input;
    END IF;
    IF valid IS NOT TRUE THEN
        RAISE EXCEPTION USING ERRCODE = '23514',
            MESSAGE = 'Recurring rule identity, schedule or progress is immutable',
            CONSTRAINT = 'ck_recurring_rule_identity';
    END IF;
    IF capture THEN
        SELECT * INTO source FROM public.business_documents
            WHERE id = NEW.source_document_id AND ledger_id = NEW.ledger_id;
        IF NOT FOUND OR source.document_kind IS DISTINCT FROM 'invoice'
            OR source.state IS DISTINCT FROM 'draft' OR source.archived
            OR source.version IS DISTINCT FROM NEW.source_version
            OR NEW.template_input IS DISTINCT FROM
                public.coinpup_draft_input(NEW.source_document_id, NEW.ledger_id) THEN
            RAISE EXCEPTION USING ERRCODE = '23514',
                MESSAGE = 'Recurring template must match its scoped source draft version',
                CONSTRAINT = 'ck_recurring_rule_template';
        END IF;
    END IF;
    RETURN NEW;
END; $$;
"""

INSTANCE_SQL = """
CREATE FUNCTION coinpup_guard_recurring_instance() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog AS $$
DECLARE r public.recurring_invoice_rules%ROWTYPE;
    d public.business_documents%ROWTYPE;
BEGIN
    PERFORM pg_advisory_xact_lock(18945999704708432);
    IF TG_OP <> 'INSERT' THEN
        RAISE EXCEPTION USING ERRCODE = '23514',
            MESSAGE = 'Recurring instances are immutable',
            CONSTRAINT = 'ck_recurring_instance_immutable';
    END IF;
    SELECT * INTO r FROM public.recurring_invoice_rules
        WHERE id = NEW.rule_id AND ledger_id = NEW.ledger_id;
    IF NOT FOUND OR r.archived OR r.version IS DISTINCT FROM NEW.rule_version
        OR r.next_index IS DISTINCT FROM NEW.occurrence_index
        OR NEW.version IS DISTINCT FROM 1 OR NEW.archived
        OR NEW.scheduled_date IS DISTINCT FROM public.coinpup_recurring_date(
            r.anchor_date, r.frequency, r.interval_count, NEW.occurrence_index) THEN
        RAISE EXCEPTION USING ERRCODE = '23514',
            MESSAGE = 'Recurring instance must match the current rule slot',
            CONSTRAINT = 'ck_recurring_instance_slot';
    END IF;
    SELECT * INTO d FROM public.business_documents
        WHERE id = NEW.id AND ledger_id = NEW.ledger_id;
    IF NOT FOUND OR d.document_kind IS DISTINCT FROM 'invoice'
        OR d.state IS DISTINCT FROM 'draft' OR d.archived OR d.version IS DISTINCT FROM 1
        OR d.issue_date IS DISTINCT FROM NEW.scheduled_date
        OR NEW.original_input IS DISTINCT FROM public.coinpup_draft_input(NEW.id, NEW.ledger_id)
        OR public.coinpup_recurring_input_matches(
            r.template_input, NEW.original_input, NEW.scheduled_date) IS NOT TRUE THEN
        RAISE EXCEPTION USING ERRCODE = '23514',
            MESSAGE = 'Recurring instance input must match its template and generated draft',
            CONSTRAINT = 'ck_recurring_instance_input';
    END IF;
    RETURN NEW;
END; $$;
"""

PROGRESS_SQL = """
CREATE FUNCTION coinpup_validate_recurring_progress() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog AS $$
DECLARE rule uuid; cursor_value integer; slots bigint; first_index integer; last_index integer;
BEGIN
    IF TG_TABLE_NAME = 'recurring_invoice_rules' THEN rule := NEW.id;
    ELSE rule := NEW.rule_id; END IF;
    SELECT next_index INTO cursor_value FROM public.recurring_invoice_rules
        WHERE id = rule AND ledger_id = NEW.ledger_id;
    SELECT count(*), min(occurrence_index), max(occurrence_index)
        INTO slots, first_index, last_index FROM public.recurring_invoice_instances
        WHERE rule_id = rule AND ledger_id = NEW.ledger_id;
    IF cursor_value IS NULL OR slots <> cursor_value
        OR (slots > 0 AND (first_index <> 0 OR last_index::bigint <> cursor_value::bigint - 1)) THEN
        RAISE EXCEPTION USING ERRCODE = '23514',
            MESSAGE = 'Recurring progress requires contiguous committed instances',
            CONSTRAINT = 'ck_recurring_progress';
    END IF;
    RETURN NULL;
END; $$;
"""


def _identity(table):
    return (
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("ledger_id", sa.Uuid(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("archived", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint("id", "ledger_id", name=f"uq_{table}_id_ledger"),
        sa.ForeignKeyConstraint(
            ["ledger_id"], ["ledgers.id"], name=f"fk_{table}_ledger", ondelete="RESTRICT"
        ),
        sa.CheckConstraint("version > 0", name=f"ck_{table}_version"),
    )


def upgrade():
    op.create_table(
        "recurring_invoice_rules",
        *_identity("recurring_invoice_rules"),
        sa.Column("name", sa.String(160), nullable=False),
        sa.Column("timezone_name", sa.String(100), nullable=False),
        sa.Column("anchor_date", sa.Date(), nullable=False),
        sa.Column("frequency", sa.String(5), nullable=False),
        sa.Column("interval_count", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("next_index", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("source_document_id", sa.Uuid(), nullable=False),
        sa.Column("source_version", sa.Integer(), nullable=False),
        sa.Column("template_input", postgresql.JSONB(), nullable=False),
        sa.ForeignKeyConstraint(
            ["source_document_id", "ledger_id"],
            ["business_documents.id", "business_documents.ledger_id"],
            name="fk_recurring_invoice_rules_source_ledger",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "btrim(name) <> '' AND btrim(timezone_name) <> ''",
            name="ck_recurring_invoice_rules_names",
        ),
        sa.CheckConstraint(
            "frequency IN ('day', 'week', 'month', 'year') "
            "AND interval_count BETWEEN 1 AND 120 AND next_index >= 0 "
            "AND anchor_date BETWEEN DATE '0001-01-01' AND DATE '9999-12-31'",
            name="ck_recurring_invoice_rules_calendar",
        ),
        sa.CheckConstraint(
            "source_version > 0 AND jsonb_typeof(template_input) = 'object'",
            name="ck_recurring_invoice_rules_template",
        ),
    )
    op.create_table(
        "recurring_invoice_instances",
        *_identity("recurring_invoice_instances"),
        sa.Column("rule_id", sa.Uuid(), nullable=False),
        sa.Column("occurrence_index", sa.Integer(), nullable=False),
        sa.Column("scheduled_date", sa.Date(), nullable=False),
        sa.Column("rule_version", sa.Integer(), nullable=False),
        sa.Column("original_input", postgresql.JSONB(), nullable=False),
        sa.UniqueConstraint(
            "rule_id", "occurrence_index", name="uq_recurring_invoice_instances_slot"
        ),
        sa.ForeignKeyConstraint(
            ["rule_id", "ledger_id"],
            ["recurring_invoice_rules.id", "recurring_invoice_rules.ledger_id"],
            name="fk_recurring_invoice_instances_rule_ledger",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["id", "ledger_id"],
            ["business_documents.id", "business_documents.ledger_id"],
            name="fk_recurring_invoice_instances_document_ledger",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "occurrence_index >= 0 AND rule_version > 0 "
            "AND scheduled_date BETWEEN DATE '0001-01-01' AND DATE '9999-12-31' "
            "AND jsonb_typeof(original_input) = 'object'",
            name="ck_recurring_invoice_instances_profile",
        ),
    )
    for sql in (CALENDAR_SQL, INPUT_SQL, MATCH_SQL, RULE_SQL, INSTANCE_SQL, PROGRESS_SQL):
        op.execute(sql)
    op.drop_constraint("ck_change_log_entity_type", "change_log", type_="check")
    op.create_check_constraint(
        "ck_change_log_entity_type", "change_log", f"entity_type IN ({_ENTITY_TYPES})"
    )
    for table, guard in zip(_TABLES, ("rule", "instance"), strict=True):
        op.create_index(f"ix_{table}_ledger_id", table, ["ledger_id"])
        op.execute(
            f"CREATE TRIGGER tr_{table}_identity BEFORE INSERT OR UPDATE OR DELETE ON {table} "
            f"FOR EACH ROW EXECUTE FUNCTION coinpup_guard_recurring_{guard}()"
        )
        op.execute(
            f"CREATE CONSTRAINT TRIGGER tr_{table}_progress AFTER INSERT OR UPDATE ON {table} "
            "DEFERRABLE INITIALLY DEFERRED FOR EACH ROW "
            "EXECUTE FUNCTION coinpup_validate_recurring_progress()"
        )
        op.execute(_CHANGE_TRIGGER_SQL[table])


def downgrade():
    op.execute("SELECT pg_advisory_xact_lock(18945999704708432)")
    op.execute(
        "LOCK TABLE recurring_invoice_rules, recurring_invoice_instances IN ACCESS EXCLUSIVE MODE"
    )
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM public.recurring_invoice_rules)
               OR EXISTS (SELECT 1 FROM public.recurring_invoice_instances)
               OR EXISTS (SELECT 1 FROM public.change_log WHERE entity_type IN
                           ('recurring_invoice_rules', 'recurring_invoice_instances')) THEN
                RAISE EXCEPTION USING ERRCODE = '23514',
                    MESSAGE = 'Recurring invoice history prevents downgrade',
                    CONSTRAINT = 'ck_recurring_downgrade';
            END IF;
        END; $$;
    """)
    for table in _TABLES:
        for trigger in (f"tr_{table}_identity", f"tr_{table}_progress", f"trg_{table}_change_log"):
            op.execute(f"DROP TRIGGER {trigger} ON {table}")
    for function in (
        "coinpup_validate_recurring_progress()",
        "coinpup_guard_recurring_instance()",
        "coinpup_guard_recurring_rule()",
        "coinpup_recurring_input_matches(jsonb, jsonb, date)",
        "coinpup_draft_input(uuid, uuid)",
        "coinpup_recurring_date(date, text, integer, integer)",
    ):
        op.execute(f"DROP FUNCTION {function}")
    for table in reversed(_TABLES):
        op.drop_table(table)
    op.drop_constraint("ck_change_log_entity_type", "change_log", type_="check")
    op.create_check_constraint(
        "ck_change_log_entity_type", "change_log", f"entity_type IN ({_OLD_ENTITY_TYPES})"
    )
