"""Reserve stable source identities without changing original confirmed line facts or receipts."""

from alembic import op

revision = "20261110_0077"
down_revision = "20261110_0076"
branch_labels = None
depends_on = None


_OLD_LINE = r"""
CREATE OR REPLACE FUNCTION coinpup_guard_draft_line() RETURNS trigger
    LANGUAGE plpgsql SET search_path = pg_catalog AS $$
    DECLARE mutable_fields text[] := ARRAY[
        'version', 'archived', 'updated_at', 'description', 'asset_id', 'quantity', 'unit_price',
        'discount_amount', 'tax_rate_percent', 'category_id', 'category_kind', 'project_id',
        'recognition_date', 'category_snapshot', 'project_snapshot',
        'net_amount', 'tax_amount', 'total_amount'
    ]; parent_state text; precision_digits integer; q numeric; p numeric; d numeric; r numeric;
        n numeric; t numeric;
    BEGIN
        IF TG_OP IN ('INSERT','UPDATE') AND EXISTS (
            SELECT 1 FROM public.business_documents d
            WHERE d.id=NEW.document_id AND d.ledger_id=NEW.ledger_id AND d.state='confirmed'
        ) THEN
            RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='Line requires a draft document',
                CONSTRAINT='ck_business_line_draft';
        END IF;
        IF TG_OP = 'INSERT' THEN
            IF (NEW.version = 1 AND NOT NEW.archived) IS NOT TRUE THEN
                RAISE EXCEPTION USING ERRCODE = '23514',
                    MESSAGE = 'Business reference identity or version is immutable',
                    CONSTRAINT = 'ck_business_reference_identity';
            END IF;
        ELSIF TG_OP = 'UPDATE' AND NEW.version::bigint = OLD.version::bigint + 1
            AND (to_jsonb(NEW) - mutable_fields) IS NOT DISTINCT FROM
                (to_jsonb(OLD) - mutable_fields)
            AND ((OLD.quantity IS NULL AND NEW.quantity IS NULL)
                 OR (OLD.quantity IS NOT NULL AND NEW.quantity IS NOT NULL)) THEN
            NULL;
        ELSE
            RAISE EXCEPTION USING ERRCODE = '23514',
                MESSAGE = 'Business reference identity or version is immutable',
                CONSTRAINT = 'ck_business_reference_identity';
        END IF;
        IF NEW.quantity IS NULL THEN RETURN NEW; END IF;
        SELECT state INTO parent_state FROM public.business_documents
            WHERE id = NEW.document_id AND ledger_id = NEW.ledger_id;
        IF FOUND AND parent_state IS DISTINCT FROM 'draft' THEN
            RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'Line requires a draft document',
                CONSTRAINT = 'ck_business_line_draft';
        END IF;
        SELECT scale INTO precision_digits FROM public.assets WHERE asset_id = NEW.asset_id;
        IF NOT FOUND THEN RETURN NEW; END IF;
        IF NEW.unit_price IS NULL OR NEW.discount_amount IS NULL
            OR NEW.tax_rate_percent IS NULL THEN RETURN NEW; END IF;
        IF NEW.quantity !~ '^(0|[1-9][0-9]{0,19})(\.[0-9]{1,18})?$'
           OR NEW.tax_rate_percent !~ '^(0|[1-9][0-9]{0,19})(\.[0-9]{1,18})?$'
           OR NEW.unit_price !~ '^-?(0|[1-9][0-9]{0,19})(\.[0-9]{1,18})?$'
           OR NEW.discount_amount !~ '^-?(0|[1-9][0-9]{0,19})(\.[0-9]{1,18})?$'
           OR length(split_part(NEW.unit_price, '.', 2)) > precision_digits
           OR length(split_part(NEW.discount_amount, '.', 2)) > precision_digits THEN
            RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'Invalid exact draft line input',
                CONSTRAINT = 'ck_business_line_pricing';
        END IF;
        q := NEW.quantity::numeric; p := NEW.unit_price::numeric;
        d := NEW.discount_amount::numeric; r := NEW.tax_rate_percent::numeric;
        n := round(q * p - d, precision_digits); t := round(n * r * 0.01, precision_digits);
        IF q <= 0 OR p < 0 OR d < 0 OR d > q * p
           OR NEW.net_amount IS DISTINCT FROM n OR NEW.tax_amount IS DISTINCT FROM t
           OR NEW.total_amount IS DISTINCT FROM n + t THEN
            RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'Invalid exact draft line input',
                CONSTRAINT = 'ck_business_line_pricing';
        END IF;
        RETURN NEW;
    END; $$
"""

_OLD_SNAPSHOT = r"""
CREATE OR REPLACE FUNCTION coinpup_business_source_snapshot(document_identifier uuid)
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

_OLD_COMPLETE = r"""
CREATE OR REPLACE FUNCTION coinpup_validate_business_confirmation(confirmation_identifier uuid)
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

# Only an actual, scoped replacement fact may register an identity on an already sealed
# document. The existing draft guard still rejects every edit and every priced insert.
_LINE = _OLD_LINE.replace(
    "IF TG_OP IN ('INSERT','UPDATE') AND EXISTS (",
    """IF TG_OP = 'INSERT' AND NEW.quantity IS NULL AND NEW.version = 1 AND NOT NEW.archived
           AND EXISTS (SELECT 1 FROM public.business_document_revision_lines f
               WHERE (f.source_line_id,f.document_id,f.ledger_id) =
                     (NEW.id,NEW.document_id,NEW.ledger_id)) THEN
            RETURN NEW;
        END IF;
        IF TG_OP IN ('INSERT','UPDATE') AND EXISTS (""",
)
# Exclude only the registered, unpriced identity rows. The original confirmation still
# rejects a generic unpriced line, counts zero-price lines and validates its original 201.
_ORIGINAL = """ AND (l.quantity IS NOT NULL OR NOT EXISTS (
       SELECT 1 FROM public.business_document_revision_lines f
       WHERE (f.source_line_id,f.document_id,f.ledger_id)=(l.id,l.document_id,l.ledger_id)))"""
_SNAPSHOT = _OLD_SNAPSHOT.replace("AND NOT l.archived", "AND NOT l.archived" + _ORIGINAL)
_COMPLETE = _OLD_COMPLETE.replace(
    "FROM public.business_document_lines\n      WHERE",
    "FROM public.business_document_lines l\n      WHERE",
).replace("AND NOT archived;", "AND NOT archived" + _ORIGINAL + ";")

_BIND = """
CREATE FUNCTION coinpup_bind_document_revision_source() RETURNS trigger
LANGUAGE plpgsql SET search_path=pg_catalog AS $$
DECLARE source public.business_document_lines%ROWTYPE;
BEGIN
    SELECT * INTO source FROM public.business_document_lines WHERE id=NEW.source_line_id;
    IF NOT FOUND THEN
        INSERT INTO public.business_document_lines (id,document_id,ledger_id)
        VALUES (NEW.source_line_id,NEW.document_id,NEW.ledger_id)
        ON CONFLICT (id) DO NOTHING;
        SELECT * INTO source FROM public.business_document_lines WHERE id=NEW.source_line_id;
    END IF;
    IF (source.document_id,source.ledger_id) IS DISTINCT FROM (NEW.document_id,NEW.ledger_id)
        OR source.archived THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='Source line has a different origin',
            CONSTRAINT='ck_document_revision_line_source';
    END IF;
    RETURN NEW;
END; $$;
"""


def upgrade():
    op.execute("SELECT pg_advisory_xact_lock(18945999704708432)")
    op.create_index(
        "ix_document_revision_line_source",
        "business_document_revision_lines",
        ["source_line_id", "document_id", "ledger_id"],
    )
    op.create_foreign_key(
        "fk_document_revision_line_source",
        "business_document_revision_lines",
        "business_document_lines",
        ["source_line_id", "document_id", "ledger_id"],
        ["id", "document_id", "ledger_id"],
        deferrable=True,
        initially="DEFERRED",
    )
    for definition in (_LINE, _SNAPSHOT, _COMPLETE, _BIND):
        op.execute(definition)
    op.execute(
        "CREATE TRIGGER tr_document_revision_line_source AFTER INSERT "
        "ON business_document_revision_lines FOR EACH ROW "
        "EXECUTE FUNCTION coinpup_bind_document_revision_source()"
    )


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
    op.execute("DROP TRIGGER tr_document_revision_line_source ON business_document_revision_lines")
    op.execute("DROP FUNCTION coinpup_bind_document_revision_source()")
    for definition in (_OLD_LINE, _OLD_SNAPSHOT, _OLD_COMPLETE):
        op.execute(definition)
    op.drop_constraint(
        "fk_document_revision_line_source", "business_document_revision_lines", type_="foreignkey"
    )
    op.drop_index("ix_document_revision_line_source", table_name="business_document_revision_lines")
