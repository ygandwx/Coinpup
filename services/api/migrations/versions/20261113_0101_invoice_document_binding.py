"""Bind every rendered document fact to the actual current immutable Invoice version."""

from alembic import op

revision = "20261113_0101"
down_revision = "20261113_0100"
branch_labels = None
depends_on = None

_PROJECTION = r"""
CREATE FUNCTION coinpup_invoice_document_input(ledger_identifier uuid, document_identifier uuid,
    requested_version integer) RETURNS jsonb
LANGUAGE plpgsql STABLE SET search_path=pg_catalog AS $$
DECLARE d public.business_documents%ROWTYPE; c public.business_confirmations%ROWTYPE;
    r public.business_document_revisions%ROWTYPE; a public.assets%ROWTYPE;
    g jsonb; h jsonb; item jsonb; facts jsonb; members jsonb := '[]'; postings jsonb := '[]';
BEGIN
    SELECT * INTO d FROM public.business_documents
        WHERE (id,ledger_id)=(document_identifier,ledger_identifier);
    SELECT * INTO c FROM public.business_confirmations
        WHERE (document_id,ledger_id)=(document_identifier,ledger_identifier);
    SELECT * INTO r FROM public.business_document_revisions
        WHERE (document_id,ledger_id,confirmation_id)=(d.id,d.ledger_id,c.id)
        ORDER BY document_version DESC LIMIT 1;
    IF d.id IS NULL OR c.id IS NULL OR d.archived OR d.state IS DISTINCT FROM 'confirmed'
       OR d.document_kind IS DISTINCT FROM 'invoice'
       OR requested_version IS DISTINCT FROM COALESCE(r.document_version,d.version)
       OR (r.id IS NOT NULL AND r.action IS DISTINCT FROM 'correct') THEN
        RETURN NULL;
    END IF;
    g := public.coinpup_confirmed_document_group(ledger_identifier,d.id,requested_version);
    IF g IS NULL THEN RETURN NULL; END IF;
    SELECT * INTO a FROM public.assets WHERE asset_id=g->>'asset_id';
    IF a.asset_id IS NULL THEN RETURN NULL; END IF;
    h := to_jsonb(d);
    IF r.id IS NOT NULL THEN
        h := h || jsonb_build_object('version',r.document_version,'updated_at',r.created_at,
            'party_id',r.party_id,'asset_id',r.asset_id,'issue_date',r.issue_date,
            'due_date',r.due_date,'notes',r.notes,'party_snapshot',r.party_snapshot);
    END IF;
    FOR item IN SELECT value FROM jsonb_array_elements(g->'lines') LOOP
        IF r.id IS NULL THEN
            SELECT to_jsonb(l) INTO facts FROM public.business_document_lines l
                WHERE (l.id,l.document_id,l.ledger_id)=
                    ((item->>'id')::uuid,d.id,d.ledger_id);
        ELSE
            SELECT (to_jsonb(f)-ARRAY['revision_id','confirmation_id','document_version',
                'source_line_id']) || jsonb_build_object('id',f.source_line_id,
                    'asset_id',r.asset_id,'updated_at',f.created_at,'archived',false)
                INTO facts FROM public.business_document_revision_lines f
                WHERE (f.source_line_id,f.revision_id,f.document_id,f.ledger_id)=
                    ((item->>'id')::uuid,r.id,d.id,d.ledger_id);
        END IF;
        IF facts IS NULL THEN RETURN NULL; END IF;
        facts := facts || jsonb_build_object('net_amount',item->'net_amount',
            'tax_amount',item->'tax_amount','total_amount',item->'total_amount');
        members := members || jsonb_build_array(facts);
        IF item->'posting' IS DISTINCT FROM 'null'::jsonb THEN
            postings := postings || jsonb_build_array((item->'posting') ||
                jsonb_build_object('source_line_id',item->'id'));
        END IF;
    END LOOP;
    h := h || jsonb_build_object('confirmation_id',c.id,'confirmed_at',c.created_at,
        'lines',members,'line_count',jsonb_array_length(members),'net_amount',g->'net_amount',
        'tax_amount',g->'tax_amount','total_amount',g->'total_amount');
    RETURN jsonb_build_object('schema_version',1,'asset',jsonb_build_object(
        'code',a.code,'kind',a.kind,'scale',a.scale,'network',a.network,
        'token_reference',a.token_reference),'current',jsonb_build_object(
        'ledger_id',d.ledger_id,'document_id',d.id,'confirmation_id',c.id,
        'document_kind',d.document_kind,'version',requested_version,'status','active',
        'revision_id',r.id,'document',h,'postings',postings));
END; $$;
"""

_NORMALIZE = r"""
CREATE FUNCTION coinpup_invoice_timestamp(value jsonb) RETURNS timestamptz
LANGUAGE plpgsql STABLE STRICT SET search_path=pg_catalog AS $$
DECLARE stamp text := value#>>'{}';
BEGIN
    IF jsonb_typeof(value) IS DISTINCT FROM 'string'
       OR stamp !~ ('^[0-9]{4}-[0-9]{2}-[0-9]{2}T([01][0-9]|2[0-3]):[0-5][0-9]:' ||
           '[0-5][0-9](\.[0-9]{6})?(Z|[+-][0-9]{2}:[0-5][0-9])$')
       OR stamp ~ '[+-]00:00$' OR stamp ~ '\.000000(Z|[+-][0-9]{2}:[0-9]{2})$' THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='Invoice timestamp is not canonical',
            CONSTRAINT='ck_invoice_document_binding';
    END IF;
    RETURN stamp::timestamptz;
END; $$;

CREATE FUNCTION coinpup_invoice_document_timestamps(input jsonb) RETURNS jsonb
LANGUAGE plpgsql STABLE STRICT SET search_path=pg_catalog AS $$
DECLARE d jsonb; members jsonb;
BEGIN
    d := input#>'{current,document}';
    IF jsonb_typeof(d) IS DISTINCT FROM 'object'
       OR jsonb_typeof(d->'lines') IS DISTINCT FROM 'array' THEN RETURN NULL; END IF;
    -- ISO Z and an equivalent explicit timezone offset represent the same stored instant.
    -- All other keys, JSON types, array order and original amount strings remain untouched.
    d := d || jsonb_build_object('created_at',public.coinpup_invoice_timestamp(d->'created_at'),
        'updated_at',public.coinpup_invoice_timestamp(d->'updated_at'),
        'confirmed_at',public.coinpup_invoice_timestamp(d->'confirmed_at'));
    SELECT COALESCE(jsonb_agg(value || jsonb_build_object(
        'created_at',public.coinpup_invoice_timestamp(value->'created_at'),
        'updated_at',public.coinpup_invoice_timestamp(value->'updated_at'))
        ORDER BY position),'[]'::jsonb)
        INTO members FROM jsonb_array_elements(d->'lines') WITH ORDINALITY AS x(value,position);
    RETURN jsonb_set(input,'{current,document}',jsonb_set(d,'{lines}',members));
END; $$;
"""

_BIND = r"""
CREATE FUNCTION coinpup_bind_invoice_document() RETURNS trigger
LANGUAGE plpgsql SET search_path=pg_catalog AS $$
DECLARE expected jsonb; provided jsonb;
BEGIN
    PERFORM public.coinpup_invoice_timestamp(NEW.snapshot_json::jsonb#>'{profile,created_at}');
    PERFORM public.coinpup_invoice_timestamp(NEW.snapshot_json::jsonb#>'{profile,updated_at}');
    expected := public.coinpup_invoice_document_input(
        NEW.ledger_id,NEW.document_id,NEW.document_version);
    provided := public.coinpup_invoice_document_timestamps(NEW.snapshot_json::jsonb->'document');
    IF expected IS NULL OR provided IS DISTINCT FROM expected THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='Invoice source snapshot differs',
            CONSTRAINT='ck_invoice_document_binding';
    END IF;
    RETURN NEW;
EXCEPTION WHEN data_exception THEN
    RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='Invoice source snapshot is invalid',
        CONSTRAINT='ck_invoice_document_binding';
END; $$;
"""


def upgrade():
    op.execute("SELECT pg_advisory_xact_lock(18945999704708432)")
    op.execute(_PROJECTION)
    op.execute(_NORMALIZE)
    op.execute(_BIND)
    # Alphabetic order is deliberate: numbering acquires the write lock first,
    # then canonical input binding runs, then the current document is re-read.
    op.execute(
        "CREATE TRIGGER tr_invoice_snapshot_document BEFORE INSERT "
        "ON business_invoice_reservations "
        "FOR EACH ROW EXECUTE FUNCTION coinpup_bind_invoice_document()"
    )


def downgrade():
    op.execute("SELECT pg_advisory_xact_lock(18945999704708432)")
    op.execute("LOCK TABLE business_invoice_reservations IN ACCESS EXCLUSIVE MODE")
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM public.business_invoice_reservations) OR EXISTS (
                SELECT 1 FROM public.change_log WHERE entity_type='business_invoice_reservations'
            ) THEN
                RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='Invoice document history exists',
                    CONSTRAINT='ck_invoice_document_downgrade';
            END IF;
        END; $$;
    """)
    op.execute("DROP TRIGGER tr_invoice_snapshot_document ON business_invoice_reservations")
    op.execute("DROP FUNCTION coinpup_bind_invoice_document()")
    op.execute("DROP FUNCTION coinpup_invoice_document_timestamps(jsonb)")
    op.execute("DROP FUNCTION coinpup_invoice_timestamp(jsonb)")
    op.execute("DROP FUNCTION coinpup_invoice_document_input(uuid,uuid,integer)")
