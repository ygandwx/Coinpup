"""Bind one complete bilingual PDF result to immutable Invoice intent; allocation stays closed."""

from alembic import op

revision = "20261114_0104"
down_revision = "20261114_0103"
branch_labels = None
depends_on = None

_TABLES = ("business_invoice_completions",)
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
    "business_invoice_reservations",
)
_ENTITY_TYPES = ", ".join(repr(value) for value in _OLD_ENTITY_TYPES + _TABLES)
_CHANGE_TRIGGER_SQL = {
    table: f"CREATE TRIGGER trg_{table}_change_log AFTER INSERT OR UPDATE ON {table} "
    "FOR EACH ROW EXECUTE FUNCTION coinpup_record_change()"
    for table in _TABLES
}

_SCHEMA = r"""
CREATE TABLE business_invoice_completions (
    id UUID NOT NULL PRIMARY KEY,
    ledger_id UUID NOT NULL,
    version INTEGER DEFAULT '1' NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    zh_file_id UUID NOT NULL,
    en_file_id UUID NOT NULL,
    created_by UUID NOT NULL,
    response JSONB NOT NULL,
    response_status INTEGER DEFAULT '201' NOT NULL,
    CONSTRAINT uq_invoice_completion_id_ledger UNIQUE(id,ledger_id),
    CONSTRAINT fk_invoice_completion_ledger FOREIGN KEY(ledger_id) REFERENCES ledgers(id)
        ON DELETE RESTRICT,
    CONSTRAINT ck_invoice_completion_version CHECK(version=1),
    CONSTRAINT fk_invoice_completion_reservation FOREIGN KEY(id,ledger_id)
        REFERENCES business_invoice_reservations(id,ledger_id) ON DELETE RESTRICT,
    CONSTRAINT fk_invoice_completion_owner FOREIGN KEY(ledger_id,created_by)
        REFERENCES ledgers(id,owner_id) ON DELETE RESTRICT,
    CONSTRAINT fk_invoice_completion_zh_file FOREIGN KEY(zh_file_id,ledger_id)
        REFERENCES stored_files(id,ledger_id) ON DELETE RESTRICT,
    CONSTRAINT fk_invoice_completion_en_file FOREIGN KEY(en_file_id,ledger_id)
        REFERENCES stored_files(id,ledger_id) ON DELETE RESTRICT,
    CONSTRAINT uq_invoice_completion_zh_file UNIQUE(zh_file_id),
    CONSTRAINT uq_invoice_completion_en_file UNIQUE(en_file_id),
    CONSTRAINT ck_invoice_completion_distinct_files CHECK(zh_file_id<>en_file_id),
    CONSTRAINT ck_invoice_completion_status CHECK(response_status=201),
    CONSTRAINT ck_invoice_completion_response_size CHECK(jsonb_typeof(response)='object'
        AND octet_length(response::text)<=65536)
);
"""

_RESPONSE = r"""
CREATE FUNCTION coinpup_invoice_completion_response(ledger_identifier uuid,
    reservation_identifier uuid, zh_identifier uuid, en_identifier uuid) RETURNS jsonb
LANGUAGE sql STABLE SET search_path=pg_catalog AS $$
SELECT jsonb_build_object('kind','invoice_issuance','state','complete','id',r.id,
    'ledger_id',r.ledger_id,'document_id',r.document_id,'document_version',r.document_version,
    'configuration_id',r.configuration_id,'configuration_version',r.configuration_version,
    'previous_issuance_id',r.previous_issuance_id,'numbering',r.snapshot_json::jsonb#>'{profile,numbering}',
    'sequence',r.sequence::text,'number',r.number,'snapshot_sha256',r.snapshot_sha256,
    'template_version',r.template_version,'artifacts',jsonb_build_array(
        jsonb_build_object('language','zh','file_id',z.id,'sha256',z.sha256,
            'byte_size',z.byte_size,'media_type',z.detected_media_type),
        jsonb_build_object('language','en','file_id',e.id,'sha256',e.sha256,
            'byte_size',e.byte_size,'media_type',e.detected_media_type)))
FROM public.business_invoice_reservations r
JOIN public.stored_files z ON
    (z.id,z.ledger_id,z.created_by)=(zh_identifier,r.ledger_id,r.created_by)
JOIN public.stored_files e ON
    (e.id,e.ledger_id,e.created_by)=(en_identifier,r.ledger_id,r.created_by)
WHERE (r.id,r.ledger_id)=(reservation_identifier,ledger_identifier) AND z.id<>e.id
    AND z.detected_media_type='application/pdf' AND e.detected_media_type='application/pdf';
$$;
"""

_GUARD = r"""
CREATE FUNCTION coinpup_guard_invoice_completion() RETURNS trigger
LANGUAGE plpgsql SET search_path=pg_catalog AS $$
DECLARE reservation public.business_invoice_reservations%ROWTYPE; expected jsonb;
BEGIN
    IF TG_OP<>'INSERT' THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='Invoice completion is immutable',
            CONSTRAINT='ck_invoice_completion_immutable';
    END IF;
    PERFORM pg_advisory_xact_lock(18945999704708432);
    IF current_setting('transaction_isolation')<>'read committed' THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='Invalid Invoice completion isolation',
            CONSTRAINT='ck_invoice_completion_isolation';
    END IF;
    SELECT * INTO reservation FROM public.business_invoice_reservations
        WHERE (id,ledger_id)=(NEW.id,NEW.ledger_id);
    expected := public.coinpup_invoice_completion_response(
        NEW.ledger_id,NEW.id,NEW.zh_file_id,NEW.en_file_id);
    IF reservation.id IS NULL OR NEW.created_by IS DISTINCT FROM reservation.created_by
       OR NEW.created_at<reservation.created_at OR expected IS NULL
       OR NEW.response IS DISTINCT FROM expected
       OR EXISTS(SELECT 1 FROM public.stored_files
           WHERE id IN (NEW.zh_file_id,NEW.en_file_id) AND archived) THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='Invoice completion evidence differs',
            CONSTRAINT='ck_invoice_completion_facts';
    END IF;
    IF EXISTS(SELECT 1 FROM public.business_invoice_completions
        WHERE zh_file_id IN (NEW.zh_file_id,NEW.en_file_id)
           OR en_file_id IN (NEW.zh_file_id,NEW.en_file_id)) THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='Invoice originals are already sealed',
            CONSTRAINT='ck_invoice_completion_file_reuse';
    END IF;
    RETURN NEW;
END; $$;
"""


def upgrade():
    op.execute("SELECT pg_advisory_xact_lock(18945999704708432)")
    op.execute(_SCHEMA)
    op.execute(_RESPONSE)
    op.execute(_GUARD)
    op.execute(
        "CREATE TRIGGER tr_invoice_completion_immutable BEFORE INSERT OR UPDATE OR DELETE "
        "ON business_invoice_completions FOR EACH ROW "
        "EXECUTE FUNCTION coinpup_guard_invoice_completion()"
    )
    op.execute("ALTER TABLE change_log DROP CONSTRAINT ck_change_log_entity_type")
    op.execute(
        "ALTER TABLE change_log ADD CONSTRAINT ck_change_log_entity_type "
        f"CHECK (entity_type IN ({_ENTITY_TYPES}))"
    )
    op.execute(_CHANGE_TRIGGER_SQL["business_invoice_completions"])


def downgrade():
    op.execute("SELECT pg_advisory_xact_lock(18945999704708432)")
    op.execute("LOCK TABLE public.business_invoice_completions IN ACCESS EXCLUSIVE MODE")
    op.execute("""
        DO $$ BEGIN
            IF EXISTS(SELECT 1 FROM public.business_invoice_completions) OR EXISTS(
                SELECT 1 FROM public.change_log WHERE entity_type='business_invoice_completions'
            ) THEN
                RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='Invoice completion history exists',
                    CONSTRAINT='ck_invoice_completion_downgrade';
            END IF;
        END; $$;
    """)
    op.execute("DROP TABLE public.business_invoice_completions")
    op.execute("DROP FUNCTION public.coinpup_guard_invoice_completion()")
    op.execute("DROP FUNCTION public.coinpup_invoice_completion_response(uuid,uuid,uuid,uuid)")
    op.execute("ALTER TABLE public.change_log DROP CONSTRAINT ck_change_log_entity_type")
    previous = ", ".join(repr(value) for value in _OLD_ENTITY_TYPES)
    op.execute(
        "ALTER TABLE public.change_log ADD CONSTRAINT ck_change_log_entity_type "
        f"CHECK (entity_type IN ({previous}))"
    )
