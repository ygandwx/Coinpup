"""Require each accrual operation head to match its full document-owned history."""

from alembic import op

revision = "20261110_0081"
down_revision = "20261110_0080"
branch_labels = None
depends_on = None


# Preserve precisely the pre-revision shape, error and downgrade for untouched accruals.
_ORIGINAL = """
CREATE OR REPLACE FUNCTION coinpup_validate_business_accrual(journal_identifier uuid,
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
           (CASE required_kind WHEN 'invoice_accrual' THEN 'invoice' ELSE 'bill' END)
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
"""

_HEAD = """
CREATE FUNCTION coinpup_validate_document_operation_head(operation_identifier uuid)
RETURNS void LANGUAGE plpgsql SET search_path=pg_catalog AS $$
DECLARE o public.financial_operations%ROWTYPE; valid boolean;
BEGIN
    SELECT * INTO o FROM public.financial_operations WHERE id=operation_identifier;
    WITH members AS (
        SELECT p.ledger_id,p.document_id,p.line_id AS source_line_id,p.confirmation_id,1 AS version,
            'create'::text AS action,NULL::uuid AS previous_journal_id,p.journal_id
        FROM public.business_postings p WHERE p.operation_id=operation_identifier
        UNION ALL
        SELECT a.ledger_id,a.document_id,a.source_line_id,a.confirmation_id,a.operation_version,
            a.action,a.previous_journal_id,a.journal_id FROM public.business_document_accruals a
        WHERE a.operation_id=operation_identifier
    ), chain AS (
        SELECT m.*,row_number() OVER version_order AS position,
            lag(m.journal_id) OVER version_order AS preceding
        FROM members m WINDOW version_order AS (ORDER BY m.version)
    )
    SELECT count(*)=o.version AND count(DISTINCT (ledger_id,document_id,source_line_id,
            confirmation_id))=1 AND bool_and(ledger_id=o.ledger_id AND position=version AND
            CASE WHEN position=1 THEN action='create' AND previous_journal_id IS NULL
                ELSE action IN ('correct','cancel') AND preceding IS NOT NULL
                    AND preceding=previous_journal_id END AND
            (position<>o.version OR (o.status,o.current_journal_id) IS NOT DISTINCT FROM
                (CASE WHEN action='cancel' THEN 'cancelled' ELSE 'active' END,
                 coalesce(journal_id,previous_journal_id)))) INTO valid
    FROM chain;
    IF valid IS NOT TRUE THEN
        RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='Accrual operation has no matching head',
            CONSTRAINT='ck_document_accrual_head';
    END IF;
END; $$;
"""

# At least one registered document transition is needed before an original may lose its
# active v1 head. Each immutable posting still proves its own source and monetary facts.
_LEGACY_LOOKUP = "    SELECT * INTO member FROM public.business_postings"
_DISPATCH = _ORIGINAL.replace(
    _LEGACY_LOOKUP,
    """    IF EXISTS (SELECT 1 FROM public.business_document_accruals a
               WHERE a.operation_id=operation.id) THEN
        IF operation.kind IS DISTINCT FROM required_kind OR (
            (required_kind,control_key,account_sign) IS DISTINCT FROM
                ('invoice_accrual'::text,'receivable.customer'::text,1) AND
            (required_kind,control_key,account_sign) IS DISTINCT FROM
                ('bill_accrual'::text,'payable.supplier'::text,-1)
        ) THEN
            RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='Business accrual shape is invalid',
                CONSTRAINT='ck_business_accrual_shape';
        END IF;
        PERFORM public.coinpup_validate_document_accrual_fact(journal_identifier);
        PERFORM public.coinpup_validate_document_operation_head(operation.id);
        RETURN;
    END IF;
"""
    + _LEGACY_LOOKUP,
)

_EVENT = """
CREATE FUNCTION coinpup_check_document_operation_head() RETURNS trigger
LANGUAGE plpgsql SET search_path=pg_catalog AS $$
BEGIN
    PERFORM public.coinpup_validate_document_operation_head(NEW.operation_id);
    RETURN NULL;
END; $$;
"""

_TRIGGER = """
CREATE CONSTRAINT TRIGGER tr_document_accrual_head AFTER INSERT ON business_document_accruals
DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION coinpup_check_document_operation_head();
"""


def upgrade():
    op.execute("SELECT pg_advisory_xact_lock(18945999704708432)")
    for definition in (_HEAD, _DISPATCH, _EVENT, _TRIGGER):
        op.execute(definition)


def downgrade():
    op.execute("SELECT pg_advisory_xact_lock(18945999704708432)")
    op.execute("LOCK TABLE public.business_document_accruals IN ACCESS EXCLUSIVE MODE")
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM public.business_document_accruals)
                OR EXISTS (SELECT 1 FROM public.change_log
                    WHERE entity_type='business_document_accruals') THEN
                RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='Accrual history prevents downgrade',
                    CONSTRAINT='ck_document_accrual_head_downgrade';
            END IF;
        END; $$;
    """)
    op.execute(_ORIGINAL)
    op.execute("DROP TRIGGER tr_document_accrual_head ON business_document_accruals")
    op.execute("DROP FUNCTION coinpup_check_document_operation_head()")
    op.execute("DROP FUNCTION coinpup_validate_document_operation_head(uuid)")
