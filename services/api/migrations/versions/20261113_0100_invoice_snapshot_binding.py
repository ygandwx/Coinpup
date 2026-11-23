"""Bind exact reserved input, original request, configuration and private Logo before activation."""

from alembic import op

revision = "20261113_0100"
down_revision = "20261113_0099"
branch_labels = None
depends_on = None

_CANONICAL = r"""
CREATE FUNCTION coinpup_invoice_canonical_json(v jsonb) RETURNS text
LANGUAGE plpgsql IMMUTABLE STRICT SET search_path=pg_catalog AS $$
DECLARE result text;
BEGIN
    CASE jsonb_typeof(v)
    WHEN 'object' THEN
        SELECT '{' || COALESCE(string_agg(to_jsonb(e.key)::text || ':' ||
            public.coinpup_invoice_canonical_json(e.value),',' ORDER BY e.key COLLATE "C"),'')
            || '}' INTO result FROM jsonb_each(v) e;
    WHEN 'array' THEN
        SELECT '[' || COALESCE(string_agg(public.coinpup_invoice_canonical_json(e.value),','
            ORDER BY e.position),'') || ']' INTO result
            FROM jsonb_array_elements(v) WITH ORDINALITY AS e(value,position);
    WHEN 'number' THEN
        IF v::text !~ '^-?(0|[1-9][0-9]*)$' THEN
            RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='Invoice JSON requires exact values',
                CONSTRAINT='ck_invoice_snapshot_canonical';
        END IF;
        result := v::text;
    ELSE result := v::text;
    END CASE;
    RETURN result;
END; $$;
"""

_BIND = r"""
CREATE FUNCTION coinpup_bind_invoice_snapshot() RETURNS trigger
LANGUAGE plpgsql SET search_path=pg_catalog AS $$
DECLARE s jsonb; r jsonb; intent jsonb; normalized_request jsonb; expected_profile jsonb;
        expected_logo jsonb; envelope jsonb; p public.business_invoice_profiles%ROWTYPE;
        f public.stored_files%ROWTYPE;
        request_fields text[] := ARRAY['id','document_id','expected_document_version',
            'configuration_id','expected_configuration_version','previous_issuance_id'];
        root_fields text[] := ARRAY['schema_version','template_version','intent',
            'number','document','profile','logo'];
BEGIN
    IF (octet_length(NEW.snapshot_json) BETWEEN 2 AND 4259840
        AND octet_length(NEW.request_json) BETWEEN 2 AND 65536) IS NOT TRUE THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='Invoice input size is invalid',
            CONSTRAINT='ck_invoice_snapshot_binding';
    END IF;
    s := NEW.snapshot_json::jsonb; r := NEW.request_json::jsonb;
    IF jsonb_typeof(s) IS DISTINCT FROM 'object' OR jsonb_typeof(r) IS DISTINCT FROM 'object'
       OR NEW.snapshot_json IS DISTINCT FROM public.coinpup_invoice_canonical_json(s)
       OR NEW.request_json IS DISTINCT FROM public.coinpup_invoice_canonical_json(r)
       OR NEW.snapshot_sha256 IS DISTINCT FROM
            encode(sha256(convert_to(NEW.snapshot_json,'UTF8')),'hex')
       OR s-root_fields IS DISTINCT FROM '{}'::jsonb OR (s ?& root_fields) IS NOT TRUE
       OR s->'schema_version' IS DISTINCT FROM '1'::jsonb
       OR NEW.template_version IS DISTINCT FROM 'commercial-invoice-v1'
       OR s->>'template_version' IS DISTINCT FROM NEW.template_version
       OR (r ?& request_fields) IS NOT TRUE OR r-request_fields IS DISTINCT FROM '{}'::jsonb THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='Invoice canonical input is inconsistent',
            CONSTRAINT='ck_invoice_snapshot_binding';
    END IF;
    intent := jsonb_build_object('id',NEW.id,'document_id',NEW.document_id,
        'expected_document_version',NEW.document_version,'configuration_id',NEW.configuration_id,
        'expected_configuration_version',NEW.configuration_version,
        'previous_issuance_id',NEW.previous_issuance_id);
    normalized_request := jsonb_build_object('id',(r->>'id')::uuid,
        'document_id',(r->>'document_id')::uuid,
        'expected_document_version',r->'expected_document_version',
        'configuration_id',(r->>'configuration_id')::uuid,
        'expected_configuration_version',r->'expected_configuration_version',
        'previous_issuance_id',(r->>'previous_issuance_id')::uuid);
    envelope := jsonb_build_object('action','invoice_issuance',
        'route','/api/v1/ledgers/{ledger_id}/invoice-issuances','ledger_id',NEW.ledger_id,'payload',r);
    IF s->'intent' IS DISTINCT FROM intent OR normalized_request IS DISTINCT FROM intent
       OR NEW.request_hash IS DISTINCT FROM encode(sha256(convert_to(
            public.coinpup_invoice_canonical_json(envelope),'UTF8')),'hex')
       OR s->'number' IS DISTINCT FROM
            jsonb_build_object('sequence',NEW.sequence::text,'number',NEW.number)
       OR s#>>'{document,current,ledger_id}' IS DISTINCT FROM NEW.ledger_id::text
       OR s#>>'{document,current,document_id}' IS DISTINCT FROM NEW.document_id::text
       OR s#>'{document,current,version}' IS DISTINCT FROM to_jsonb(NEW.document_version)
       OR s#>>'{document,current,status}' IS DISTINCT FROM 'active'
       OR s#>>'{document,current,document_kind}' IS DISTINCT FROM 'invoice' THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='Invoice intent and snapshot differ',
            CONSTRAINT='ck_invoice_snapshot_intent';
    END IF;
    SELECT * INTO p FROM public.business_invoice_profiles
        WHERE (id,ledger_id,version)=(NEW.configuration_id,NEW.ledger_id,NEW.configuration_version);
    expected_profile := (to_jsonb(p) - ARRAY[
        'number_prefix','minimum_digits','created_at','updated_at']) || jsonb_build_object(
            'numbering',jsonb_build_object('prefix',p.number_prefix,'minimum_digits',p.minimum_digits));
    IF p.id IS NULL OR p.archived OR jsonb_typeof(s->'profile') IS DISTINCT FROM 'object'
       OR (s->'profile')-ARRAY['created_at','updated_at'] IS DISTINCT FROM expected_profile
       OR (s#>>'{profile,created_at}')::timestamptz IS DISTINCT FROM p.created_at
       OR (s#>>'{profile,updated_at}')::timestamptz IS DISTINCT FROM p.updated_at
       OR NEW.logo_file_id IS DISTINCT FROM p.logo_file_id THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='Invoice configuration snapshot differs',
            CONSTRAINT='ck_invoice_snapshot_profile';
    END IF;
    expected_logo := 'null'::jsonb;
    IF p.logo_file_id IS NOT NULL THEN
        SELECT * INTO f FROM public.stored_files WHERE (id,ledger_id)=(p.logo_file_id,p.ledger_id);
        IF f.id IS NULL OR f.archived OR f.version<p.logo_file_version
           OR f.detected_media_type NOT IN ('image/png','image/jpeg','image/webp') THEN
            RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='Invoice Logo is unavailable',
                CONSTRAINT='ck_invoice_snapshot_logo';
        END IF;
        expected_logo := jsonb_build_object('file_id',f.id,'ledger_id',f.ledger_id,
            'reviewed_version',p.logo_file_version,'sha256',f.sha256,'byte_size',f.byte_size,
            'media_type',f.detected_media_type);
    END IF;
    IF s->'logo' IS DISTINCT FROM expected_logo THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='Invoice Logo identity differs',
            CONSTRAINT='ck_invoice_snapshot_logo';
    END IF;
    RETURN NEW;
EXCEPTION WHEN data_exception THEN
    RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='Invoice stored input is invalid',
        CONSTRAINT='ck_invoice_snapshot_binding';
END; $$;
"""


def upgrade():
    op.execute("SELECT pg_advisory_xact_lock(18945999704708432)")
    op.execute(_CANONICAL)
    op.execute(_BIND)
    op.execute(
        "CREATE TRIGGER tr_invoice_snapshot_binding BEFORE INSERT ON business_invoice_reservations "
        "FOR EACH ROW EXECUTE FUNCTION coinpup_bind_invoice_snapshot()"
    )


def downgrade():
    op.execute("SELECT pg_advisory_xact_lock(18945999704708432)")
    op.execute("LOCK TABLE business_invoice_reservations IN ACCESS EXCLUSIVE MODE")
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM public.business_invoice_reservations) OR EXISTS (
                SELECT 1 FROM public.change_log WHERE entity_type='business_invoice_reservations'
            ) THEN
                RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='Invoice input history exists',
                    CONSTRAINT='ck_invoice_snapshot_downgrade';
            END IF;
        END; $$;
    """)
    op.execute("DROP TRIGGER tr_invoice_snapshot_binding ON business_invoice_reservations")
    op.execute("DROP FUNCTION coinpup_bind_invoice_snapshot()")
    op.execute("DROP FUNCTION coinpup_invoice_canonical_json(jsonb)")
