"""Optional ledger periods, immutable audit/receipt chain and journal date guard."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "20261009_0018"
down_revision = "20261009_0017"
branch_labels = None
depends_on = None

_TABLES = ("ledger_periods", "ledger_period_audits", "ledger_period_receipts")
_OLD_ENTITY_TYPES = (
    "'entities', 'ledgers', 'accounts', 'account_assets', 'categories', 'assets', "
    "'financial_operations', 'stored_files', 'operation_file_links', 'ocr_jobs', "
    "'ocr_drafts', 'ocr_confirmations', 'business_parties', 'business_documents', "
    "'business_document_lines'"
)
_ENTITY_TYPES = _OLD_ENTITY_TYPES + ", " + ", ".join(f"'{table}'" for table in _TABLES)
_CHANGE_TRIGGER_SQL = {
    table: f"CREATE TRIGGER trg_{table}_change_log AFTER INSERT OR UPDATE ON {table} "
    "FOR EACH ROW EXECUTE FUNCTION coinpup_record_change()"
    for table in _TABLES
}
_GUARD_SQL = """
CREATE FUNCTION coinpup_guard_period_identity() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog AS $$
BEGIN
    PERFORM pg_advisory_xact_lock(18945999704708432);
    IF TG_OP = 'INSERT' THEN
        IF TG_TABLE_NAME <> 'ledger_periods' OR NEW.version = 2 THEN RETURN NEW; END IF;
    ELSIF TG_OP = 'UPDATE' AND TG_TABLE_NAME = 'ledger_periods' THEN
        IF NEW.version::bigint = OLD.version::bigint + 1
           AND (to_jsonb(NEW) - ARRAY['version', 'closed_through']) IS NOT DISTINCT FROM
               (to_jsonb(OLD) - ARRAY['version', 'closed_through']) THEN RETURN NEW; END IF;
    END IF;
    RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'Period history is immutable',
        CONSTRAINT = 'ck_period_identity';
END; $$;

CREATE FUNCTION coinpup_validate_period_history() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog AS $$
DECLARE state public.ledger_periods%ROWTYPE; last_date date; last_version integer := 1;
        audit public.ledger_period_audits%ROWTYPE;
BEGIN
    SELECT * INTO state FROM public.ledger_periods WHERE ledger_id = NEW.ledger_id;
    IF NOT FOUND THEN
        RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'Period history is incomplete',
            CONSTRAINT = 'ck_period_history';
    END IF;
    FOR audit IN SELECT * FROM public.ledger_period_audits
                 WHERE ledger_id = state.ledger_id ORDER BY version LOOP
        IF audit.period_id <> state.id OR audit.version::bigint <> last_version::bigint + 1
           OR audit.previous_closed_through IS DISTINCT FROM last_date
           OR NOT EXISTS (SELECT 1 FROM public.ledger_period_receipts
                          WHERE audit_id = audit.id AND ledger_id = audit.ledger_id) THEN
            RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'Period history is incomplete',
                CONSTRAINT = 'ck_period_history';
        END IF;
        last_date := audit.closed_through;
        last_version := audit.version;
    END LOOP;
    IF last_version <> state.version OR last_date IS DISTINCT FROM state.closed_through THEN
        RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'Period history is incomplete',
            CONSTRAINT = 'ck_period_history';
    END IF;
    RETURN NULL;
END; $$;

CREATE FUNCTION coinpup_guard_journal_period() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog AS $$
DECLARE cutoff date;
BEGIN
    PERFORM pg_advisory_xact_lock(18945999704708432);
    SELECT closed_through INTO cutoff FROM public.ledger_periods WHERE ledger_id = NEW.ledger_id;
    IF NEW.transaction_date <= cutoff OR NEW.recognition_date <= cutoff THEN
        RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'The journal period is closed',
            CONSTRAINT = 'ck_journal_period_closed';
    END IF;
    RETURN NEW;
END; $$;
"""


def upgrade():
    op.execute("SELECT pg_advisory_xact_lock(18945999704708432)")
    for table in _TABLES:
        if table == "ledger_periods":
            extra = [
                sa.Column("closed_through", sa.Date()),
                sa.UniqueConstraint("ledger_id", name="uq_ledger_periods_ledger"),
                sa.CheckConstraint("version >= 2", name="ck_ledger_periods_version"),
            ]
        elif table == "ledger_period_audits":
            extra = [
                sa.Column("period_id", sa.Uuid(), nullable=False),
                sa.Column("actor_id", sa.Uuid(), nullable=False),
                sa.Column("action", sa.String(6), nullable=False),
                sa.Column("reason", sa.String(1000), nullable=False),
                sa.Column("previous_closed_through", sa.Date()),
                sa.Column("closed_through", sa.Date()),
                sa.UniqueConstraint("ledger_id", "version", name="uq_ledger_period_audits_version"),
                sa.ForeignKeyConstraint(
                    ["period_id", "ledger_id"],
                    ["ledger_periods.id", "ledger_periods.ledger_id"],
                    name="fk_period_audit_state",
                    ondelete="RESTRICT",
                ),
                sa.ForeignKeyConstraint(
                    ["ledger_id", "actor_id"],
                    ["ledgers.id", "ledgers.owner_id"],
                    name="fk_period_audit_actor",
                    ondelete="RESTRICT",
                ),
                sa.CheckConstraint("version >= 2", name="ck_ledger_period_audits_version"),
                sa.CheckConstraint("btrim(reason) <> ''", name="ck_period_audit_reason"),
                sa.CheckConstraint(
                    "(action = 'close' AND closed_through IS NOT NULL AND "
                    "(previous_closed_through IS NULL OR "
                    "closed_through >= previous_closed_through)) "
                    "OR (action = 'reopen' AND previous_closed_through IS NOT NULL AND "
                    "(closed_through IS NULL OR closed_through < previous_closed_through))",
                    name="ck_period_audit_transition",
                ),
            ]
        else:
            extra = [
                sa.Column("audit_id", sa.Uuid(), nullable=False),
                sa.Column("idempotency_key", sa.String(128), nullable=False),
                sa.Column("hash_version", sa.Integer(), nullable=False),
                sa.Column("request_hash", sa.String(64), nullable=False),
                sa.Column("response", JSONB(), nullable=False),
                sa.UniqueConstraint("ledger_id", "idempotency_key", name="uq_period_receipt_key"),
                sa.UniqueConstraint("audit_id", name="uq_period_receipt_audit"),
                sa.ForeignKeyConstraint(
                    ["audit_id", "ledger_id"],
                    ["ledger_period_audits.id", "ledger_period_audits.ledger_id"],
                    name="fk_period_receipt_audit",
                    ondelete="RESTRICT",
                ),
                sa.CheckConstraint(
                    "version = 1 AND hash_version = 2", name="ck_period_receipt_version"
                ),
                sa.CheckConstraint(
                    "request_hash ~ '^[0-9a-f]{64}$'", name="ck_period_receipt_hash"
                ),
                sa.CheckConstraint(
                    "idempotency_key ~ '^[!-~]{1,128}$'", name="ck_period_receipt_key"
                ),
                sa.CheckConstraint(
                    "jsonb_typeof(response) = 'object'", name="ck_period_receipt_response"
                ),
            ]
        op.create_table(
            table,
            sa.Column("id", sa.Uuid(), primary_key=True),
            sa.Column("ledger_id", sa.Uuid(), nullable=False),
            sa.Column("version", sa.Integer(), nullable=False),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.func.now(),
            ),
            sa.UniqueConstraint("id", "ledger_id", name=f"uq_{table}_id_ledger"),
            sa.ForeignKeyConstraint(
                ["ledger_id"], ["ledgers.id"], name=f"fk_{table}_ledger", ondelete="RESTRICT"
            ),
            *extra,
        )
        op.create_index(f"ix_{table}_ledger_id", table, ["ledger_id"])
    op.execute(_GUARD_SQL)
    for table in _TABLES:
        op.execute(
            f"CREATE TRIGGER tr_{table}_identity BEFORE INSERT OR UPDATE OR DELETE "
            f"ON {table} FOR EACH ROW EXECUTE FUNCTION coinpup_guard_period_identity()"
        )
        op.execute(
            f"CREATE CONSTRAINT TRIGGER tr_{table}_history AFTER INSERT OR UPDATE "
            f"ON {table} DEFERRABLE INITIALLY DEFERRED "
            "FOR EACH ROW EXECUTE FUNCTION coinpup_validate_period_history()"
        )
        op.execute(_CHANGE_TRIGGER_SQL[table])
    op.execute(
        "CREATE TRIGGER tr_journals_period BEFORE INSERT ON journals "
        "FOR EACH ROW EXECUTE FUNCTION coinpup_guard_journal_period()"
    )
    op.drop_constraint("ck_change_log_entity_type", "change_log", type_="check")
    op.create_check_constraint(
        "ck_change_log_entity_type", "change_log", f"entity_type IN ({_ENTITY_TYPES})"
    )


def downgrade():
    op.execute("SELECT pg_advisory_xact_lock(18945999704708432)")
    op.execute("LOCK TABLE " + ", ".join(_TABLES) + " IN ACCESS EXCLUSIVE MODE")
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM public.ledger_periods)
               OR EXISTS (SELECT 1 FROM public.ledger_period_audits)
               OR EXISTS (SELECT 1 FROM public.ledger_period_receipts)
               OR EXISTS (SELECT 1 FROM public.change_log WHERE entity_type IN
                   ('ledger_periods', 'ledger_period_audits', 'ledger_period_receipts')) THEN
                RAISE EXCEPTION USING ERRCODE = '23514',
                    MESSAGE = 'Period history prevents downgrade',
                    CONSTRAINT = 'ck_period_downgrade';
            END IF;
        END; $$
    """)
    op.execute("DROP TRIGGER tr_journals_period ON journals")
    op.execute("DROP FUNCTION coinpup_guard_journal_period()")
    for table in reversed(_TABLES):
        op.drop_table(table)
    op.execute("DROP FUNCTION coinpup_guard_period_identity()")
    op.execute("DROP FUNCTION coinpup_validate_period_history()")
    op.drop_constraint("ck_change_log_entity_type", "change_log", type_="check")
    op.create_check_constraint(
        "ck_change_log_entity_type", "change_log", f"entity_type IN ({_OLD_ENTITY_TYPES})"
    )
