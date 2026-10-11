"""Immutable business confirmation storage; activation follows in a separate migration."""

from alembic import op

revision = "20261011_0027"
down_revision = "20261011_0026"
branch_labels = None
depends_on = None

_TABLES = ("business_confirmations", "business_postings")
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
    "'reminder_delivery_configs', "
    "'reminder_delivery_revisions', "
).removesuffix(", ")
_ENTITY_TYPES = _OLD_ENTITY_TYPES + ", 'business_confirmations', 'business_postings'"
_CHANGE_TRIGGER_SQL = {
    table: f"CREATE TRIGGER trg_{table}_change_log AFTER INSERT OR UPDATE ON {table} "
    "FOR EACH ROW EXECUTE FUNCTION coinpup_record_change()"
    for table in _TABLES
}

_SCHEMA_SQL = """

CREATE TABLE business_confirmations (
    id UUID NOT NULL,
    ledger_id UUID NOT NULL,
    document_id UUID NOT NULL,
    created_by UUID NOT NULL,
    source_version INTEGER NOT NULL,
    idempotency_key VARCHAR(128) NOT NULL,
    snapshot JSONB NOT NULL,
    version INTEGER DEFAULT '1' NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    PRIMARY KEY (id),
    CONSTRAINT uq_business_confirmation_scope UNIQUE (id, document_id, ledger_id),
    CONSTRAINT uq_business_confirmation_document UNIQUE (document_id),
    CONSTRAINT uq_business_confirmation_key UNIQUE (ledger_id, idempotency_key),
    CONSTRAINT fk_business_confirmation_document FOREIGN KEY(document_id, ledger_id)
    REFERENCES business_documents (id, ledger_id) ON DELETE RESTRICT,
    CONSTRAINT fk_business_confirmation_owner FOREIGN KEY(ledger_id, created_by)
    REFERENCES ledgers (id, owner_id) ON DELETE RESTRICT,
    CONSTRAINT fk_business_confirmation_receipt FOREIGN KEY(ledger_id, idempotency_key)
    REFERENCES command_receipts (ledger_id, key)
    DEFERRABLE INITIALLY DEFERRED,
    CONSTRAINT ck_business_confirmation_version CHECK (version = 1 AND source_version > 0),
    CONSTRAINT ck_business_confirmation_snapshot CHECK (jsonb_typeof(snapshot) = 'object')
);
CREATE INDEX ix_business_confirmations_ledger_created
ON business_confirmations (ledger_id, created_at, id);

CREATE TABLE business_postings (
    id UUID DEFAULT gen_random_uuid() NOT NULL,
    ledger_id UUID NOT NULL,
    confirmation_id UUID NOT NULL,
    document_id UUID NOT NULL,
    line_id UUID NOT NULL,
    operation_id UUID NOT NULL,
    journal_id UUID NOT NULL,
    version INTEGER DEFAULT '1' NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    PRIMARY KEY (id),
    CONSTRAINT uq_business_posting_line UNIQUE (line_id),
    CONSTRAINT uq_business_posting_operation UNIQUE (operation_id),
    CONSTRAINT uq_business_posting_journal UNIQUE (journal_id),
    CONSTRAINT fk_business_posting_confirmation FOREIGN KEY(confirmation_id, document_id, ledger_id)
    REFERENCES business_confirmations (id, document_id, ledger_id) ON DELETE RESTRICT,
    CONSTRAINT fk_business_posting_line FOREIGN KEY(line_id, document_id, ledger_id)
    REFERENCES business_document_lines (id, document_id, ledger_id) ON DELETE RESTRICT,
    CONSTRAINT fk_business_posting_journal FOREIGN KEY(journal_id, operation_id, ledger_id)
    REFERENCES journals (id, operation_id, ledger_id)
    DEFERRABLE INITIALLY DEFERRED,
    CONSTRAINT ck_business_posting_version CHECK (version = 1)
);
CREATE INDEX ix_business_postings_confirmation ON business_postings (confirmation_id, line_id)
"""

_SNAPSHOT_SQL = """
CREATE FUNCTION coinpup_business_source_snapshot(document_identifier uuid)
RETURNS jsonb LANGUAGE sql STABLE SET search_path = pg_catalog AS $$
 SELECT jsonb_build_object(
   'document', to_jsonb(d) - ARRAY['state','version','updated_at'],
   'lines', coalesce((
     SELECT jsonb_agg(
       (to_jsonb(l) - ARRAY['net_amount','tax_amount','total_amount']) || jsonb_build_object(
         'net_amount', l.net_amount::text, 'tax_amount', l.tax_amount::text,
         'total_amount', l.total_amount::text)
       ORDER BY l.line_no NULLS LAST, l.id)
     FROM public.business_document_lines l
     WHERE l.document_id = d.id AND l.ledger_id = d.ledger_id AND NOT l.archived
   ), '[]'::jsonb)
 ) FROM public.business_documents d WHERE d.id = document_identifier;
$$;

"""

_ACCRUAL_SQL = """
CREATE FUNCTION coinpup_validate_business_accrual(journal_identifier uuid,
    required_kind text, control_key text, account_sign integer)
RETURNS void LANGUAGE plpgsql SET search_path = pg_catalog AS $$
DECLARE header public.journals%ROWTYPE; operation public.financial_operations%ROWTYPE;
        document public.business_documents%ROWTYPE; source public.business_document_lines%ROWTYPE;
        member public.business_postings%ROWTYPE;
BEGIN
    SELECT * INTO header FROM public.journals WHERE id = journal_identifier;
    SELECT * INTO operation FROM public.financial_operations WHERE id = header.operation_id;
    SELECT * INTO member FROM public.business_postings WHERE journal_id = journal_identifier;
    SELECT * INTO document FROM public.business_documents WHERE id = member.document_id;
    SELECT * INTO source FROM public.business_document_lines WHERE id = member.line_id;
    IF member.id IS NULL OR document.state IS DISTINCT FROM 'confirmed'
       OR source.total_amount IS NULL OR source.total_amount <= 0 OR source.archived
       OR document.document_kind IS DISTINCT FROM
           CASE required_kind WHEN 'invoice_accrual' THEN 'invoice' ELSE 'bill' END
       OR operation.kind IS DISTINCT FROM required_kind
       OR operation.version <> 1 OR operation.status <> 'active'
       OR operation.current_journal_id <> header.id OR header.operation_version <> 1
       OR header.journal_kind <> 'posting' OR header.reverses_journal_id IS NOT NULL
       OR header.transaction_date IS DISTINCT FROM document.issue_date
       OR header.recognition_date IS DISTINCT FROM source.recognition_date
       OR header.description IS DISTINCT FROM source.description
       OR header.revision_reason IS NOT NULL
       OR EXISTS (SELECT 1 FROM public.opening_positions WHERE operation_id=operation.id)
       OR NOT EXISTS (SELECT 1 FROM public.business_confirmations c
           WHERE c.id = member.confirmation_id AND c.document_id = document.id
               AND c.ledger_id = header.ledger_id AND c.created_by = operation.created_by)
       OR (SELECT count(*) FROM public.journal_lines WHERE journal_id = header.id) <> 2
       OR EXISTS (SELECT 1 FROM public.journal_lines l WHERE l.journal_id = header.id
           AND (l.component_no <> 0 OR l.asset_id IS DISTINCT FROM source.asset_id
               OR l.party_id IS DISTINCT FROM document.party_id
               OR l.document_id IS DISTINCT FROM document.id
               OR l.document_line_id IS DISTINCT FROM source.id
               OR l.project_id IS DISTINCT FROM source.project_id
               OR l.counterparty_entity_id IS NOT NULL OR l.dimension_owner_id IS NOT NULL))
       OR (SELECT count(*) FROM public.journal_lines l JOIN public.accounts a ON a.id=l.account_id
           WHERE l.journal_id=header.id AND l.role='account' AND a.system_key=control_key
               AND l.amount=account_sign*source.total_amount) <> 1
       OR (SELECT count(*) FROM public.journal_lines l WHERE l.journal_id=header.id
           AND l.role=source.category_kind AND l.category_id=source.category_id
               AND l.amount=-account_sign*source.total_amount) <> 1 THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='Business accrual shape is invalid',
            CONSTRAINT='ck_business_accrual_shape';
    END IF;
END; $$;

CREATE FUNCTION coinpup_validate_shape_invoice_accrual(journal_identifier uuid, common_result jsonb)
RETURNS void LANGUAGE plpgsql SET search_path = pg_catalog AS $$
BEGIN
    PERFORM public.coinpup_validate_business_accrual(
        journal_identifier, 'invoice_accrual', 'receivable.customer', 1);
END; $$;

CREATE FUNCTION coinpup_validate_shape_bill_accrual(journal_identifier uuid, common_result jsonb)
RETURNS void LANGUAGE plpgsql SET search_path = pg_catalog AS $$
BEGIN
    PERFORM public.coinpup_validate_business_accrual(
        journal_identifier, 'bill_accrual', 'payable.supplier', -1);
END; $$;

"""

_COMPLETE_SQL = """
CREATE FUNCTION coinpup_validate_business_confirmation(confirmation_identifier uuid)
RETURNS void LANGUAGE plpgsql SET search_path = pg_catalog AS $$
DECLARE confirmation public.business_confirmations%ROWTYPE;
        document public.business_documents%ROWTYPE; receipt public.command_receipts%ROWTYPE;
        positive_count integer; active_count integer; total numeric; invalid boolean;
        anchor uuid; mappings jsonb; expected_response jsonb;
BEGIN
    SELECT * INTO confirmation FROM public.business_confirmations WHERE id=confirmation_identifier;
    SELECT * INTO document FROM public.business_documents WHERE id=confirmation.document_id;
    SELECT * INTO receipt FROM public.command_receipts
        WHERE ledger_id=confirmation.ledger_id AND key=confirmation.idempotency_key;
    SELECT count(*), count(*) FILTER (WHERE total_amount>0), sum(total_amount),
        bool_or(quantity IS NULL OR asset_id IS DISTINCT FROM document.asset_id
            OR category_kind IS DISTINCT FROM
                CASE document.document_kind WHEN 'invoice' THEN 'income' ELSE 'expense' END)
      INTO active_count, positive_count, total, invalid
      FROM public.business_document_lines
      WHERE document_id=document.id AND ledger_id=document.ledger_id AND NOT archived;
    SELECT p.operation_id INTO anchor FROM public.business_postings p
      JOIN public.business_document_lines l ON l.id=p.line_id
      WHERE p.confirmation_id=confirmation.id ORDER BY l.line_no LIMIT 1;
    SELECT coalesce(jsonb_agg(jsonb_build_object('line_id',p.line_id,'line_no',l.line_no,
        'operation_id',p.operation_id,'journal_id',p.journal_id) ORDER BY l.line_no), '[]'::jsonb)
      INTO mappings FROM public.business_postings p
      JOIN public.business_document_lines l ON l.id=p.line_id
      WHERE p.confirmation_id=confirmation.id;
    expected_response := jsonb_build_object('kind','business_confirmation','id',confirmation.id,
        'ledger_id',confirmation.ledger_id,'document_id',document.id,
        'source_version',confirmation.source_version,'document_version',document.version,
        'postings',mappings);
    IF confirmation.id IS NULL OR document.state IS DISTINCT FROM 'confirmed'
       OR document.archived OR document.version::bigint <> confirmation.source_version::bigint+1
       OR confirmation.snapshot IS DISTINCT FROM
           public.coinpup_business_source_snapshot(document.id)
       OR active_count NOT BETWEEN 1 AND 200 OR invalid OR total IS NULL OR total<=0
       OR total>=100000000000000000000 OR jsonb_array_length(mappings)<>positive_count
       OR EXISTS (SELECT 1 FROM public.business_document_lines l
           WHERE l.document_id=document.id AND NOT l.archived AND l.total_amount>0
             AND NOT EXISTS (SELECT 1 FROM public.business_postings p
                 WHERE p.confirmation_id=confirmation.id AND p.line_id=l.id))
       OR EXISTS (SELECT 1 FROM public.business_postings p
           JOIN public.business_document_lines l ON l.id=p.line_id
           WHERE p.confirmation_id=confirmation.id AND (l.archived OR l.total_amount<=0))
       OR receipt.operation_id IS DISTINCT FROM anchor OR anchor IS NULL
       OR receipt.hash_version IS DISTINCT FROM 2 OR receipt.response_status IS DISTINCT FROM 201
       OR receipt.response IS DISTINCT FROM expected_response THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='Business confirmation is incomplete',
            CONSTRAINT='ck_business_confirmation_shape';
    END IF;
    -- Each posted child also receives the original deferred operation/history validation.
    PERFORM public.coinpup_validate_business_accrual(p.journal_id,
        CASE document.document_kind WHEN 'invoice' THEN 'invoice_accrual' ELSE 'bill_accrual' END,
        CASE document.document_kind
    WHEN 'invoice' THEN 'receivable.customer'  ELSE 'payable.supplier' END,
        CASE document.document_kind WHEN 'invoice' THEN 1 ELSE -1 END)
      FROM public.business_postings p WHERE p.confirmation_id=confirmation.id;
END; $$;

"""

_GUARD_SQL = """
CREATE FUNCTION coinpup_guard_business_confirmation() RETURNS trigger
LANGUAGE plpgsql SET search_path=pg_catalog AS $$
DECLARE document public.business_documents%ROWTYPE; captured jsonb;
BEGIN
    IF TG_OP <> 'INSERT' THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='Business confirmation is immutable',
            CONSTRAINT='ck_business_confirmation_immutable';
    END IF;
    PERFORM pg_advisory_xact_lock(18945999704708432);
    SELECT * INTO document FROM public.business_documents
        WHERE id=NEW.document_id AND ledger_id=NEW.ledger_id;
    IF document.state IS DISTINCT FROM 'draft' OR document.archived
       OR NEW.source_version IS DISTINCT FROM document.version THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='Confirmation requires the reviewed draft',
            CONSTRAINT='ck_business_confirmation_source';
    END IF;
    captured := public.coinpup_business_source_snapshot(document.id);
    IF NEW.snapshot IS NOT NULL AND NEW.snapshot IS DISTINCT FROM captured THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='Confirmation snapshot is inconsistent',
            CONSTRAINT='ck_business_confirmation_source';
    END IF;
    NEW.snapshot := captured;
    RETURN NEW;
END; $$;

CREATE FUNCTION coinpup_guard_business_posting() RETURNS trigger
LANGUAGE plpgsql SET search_path=pg_catalog AS $$
BEGIN
    IF TG_OP <> 'INSERT' THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='Business posting membership is immutable',
            CONSTRAINT='ck_business_posting_immutable';
    END IF;
    RETURN NEW;
END; $$;

CREATE FUNCTION coinpup_check_business_confirmation() RETURNS trigger
LANGUAGE plpgsql SET search_path=pg_catalog AS $$
DECLARE identifier uuid;
BEGIN
    IF TG_TABLE_NAME='business_confirmations' THEN identifier:=NEW.id;
    ELSIF TG_TABLE_NAME='business_postings' THEN identifier:=NEW.confirmation_id;
    ELSIF NEW.state='confirmed' THEN
        SELECT id INTO identifier FROM public.business_confirmations WHERE document_id=NEW.id;
    ELSE RETURN NULL;
    END IF;
    PERFORM public.coinpup_validate_business_confirmation(identifier);
    RETURN NULL;
END; $$;

CREATE TRIGGER tr_business_confirmation_guard BEFORE INSERT OR UPDATE OR DELETE
ON business_confirmations FOR EACH ROW EXECUTE FUNCTION coinpup_guard_business_confirmation();
CREATE TRIGGER tr_business_posting_guard BEFORE INSERT OR UPDATE OR DELETE
ON business_postings FOR EACH ROW EXECUTE FUNCTION coinpup_guard_business_posting();
CREATE CONSTRAINT TRIGGER tr_business_confirmation_complete AFTER INSERT ON business_confirmations
DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION coinpup_check_business_confirmation();
CREATE CONSTRAINT TRIGGER tr_business_posting_complete AFTER INSERT ON business_postings
DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION coinpup_check_business_confirmation();
CREATE CONSTRAINT TRIGGER tr_business_document_confirmation
    AFTER INSERT OR UPDATE ON business_documents
DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION coinpup_check_business_confirmation();

"""


def upgrade():
    op.execute("SELECT pg_advisory_xact_lock(18945999704708432)")
    for definition in (_SCHEMA_SQL, _SNAPSHOT_SQL, _ACCRUAL_SQL, _COMPLETE_SQL, _GUARD_SQL):
        op.execute(definition)
    op.drop_constraint("ck_change_log_entity_type", "change_log", type_="check")
    op.create_check_constraint(
        "ck_change_log_entity_type", "change_log", f"entity_type IN ({_ENTITY_TYPES})"
    )
    for definition in _CHANGE_TRIGGER_SQL.values():
        op.execute(definition)


def downgrade():
    op.execute("SELECT pg_advisory_xact_lock(18945999704708432)")
    op.execute("LOCK TABLE business_confirmations, business_postings IN ACCESS EXCLUSIVE MODE")
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM public.business_confirmations)
               OR EXISTS (SELECT 1 FROM public.business_postings)
               OR EXISTS (SELECT 1 FROM public.change_log WHERE entity_type IN
                   ('business_confirmations','business_postings')) THEN
                RAISE EXCEPTION USING ERRCODE='23514',
                    MESSAGE='Business confirmation history prevents downgrade',
                    CONSTRAINT='ck_business_confirmation_downgrade';
            END IF;
        END; $$;
    """)
    op.execute("DROP TRIGGER tr_business_document_confirmation ON business_documents")
    for table in reversed(_TABLES):
        op.drop_table(table)
    for function in (
        "coinpup_check_business_confirmation()",
        "coinpup_guard_business_confirmation()",
        "coinpup_guard_business_posting()",
        "coinpup_validate_business_confirmation(uuid)",
        "coinpup_validate_shape_invoice_accrual(uuid,jsonb)",
        "coinpup_validate_shape_bill_accrual(uuid,jsonb)",
        "coinpup_validate_business_accrual(uuid,text,text,integer)",
        "coinpup_business_source_snapshot(uuid)",
    ):
        op.execute(f"DROP FUNCTION {function}")
    op.drop_constraint("ck_change_log_entity_type", "change_log", type_="check")
    op.create_check_constraint(
        "ck_change_log_entity_type", "change_log", f"entity_type IN ({_OLD_ENTITY_TYPES})"
    )
