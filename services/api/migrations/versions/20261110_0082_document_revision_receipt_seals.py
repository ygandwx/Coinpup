"""Bind the permanent 200 to a complete priced revision and close its immutable row sets."""

from alembic import op

revision = "20261110_0082"
down_revision = "20261110_0081"
branch_labels = None
depends_on = None


_PROOF = """
CREATE FUNCTION coinpup_validate_document_revision_receipt(ledger_identifier uuid, intent_key text)
RETURNS void LANGUAGE plpgsql SET search_path=pg_catalog AS $$
DECLARE h public.business_document_revisions%ROWTYPE; receipt public.command_receipts%ROWTYPE;
    expected jsonb; anchor uuid;
BEGIN
    SELECT * INTO h FROM public.business_document_revisions WHERE
        (ledger_id,idempotency_key)=(ledger_identifier,intent_key);
    SELECT * INTO receipt FROM public.command_receipts WHERE
        (ledger_id,key)=(ledger_identifier,intent_key);
    IF h.id IS NULL OR receipt.hash_version IS DISTINCT FROM 2 OR
        receipt.response_status IS DISTINCT FROM 200 THEN
        RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='Missing fixed document receipt',
            CONSTRAINT='ck_document_revision_receipt';
    END IF;
    expected:=public.coinpup_confirmed_document_revision_response(h.ledger_id,h.document_id,h.id);
    SELECT (l.item->'posting'->>'operation_id')::uuid INTO anchor
    FROM jsonb_array_elements(expected->'previous'->'lines') WITH ORDINALITY AS l(item,position)
    WHERE jsonb_typeof(l.item->'posting')='object' ORDER BY l.position LIMIT 1;
    -- Compare JSONB text as well as structure: contract integers must not become 2.0.
    IF anchor IS NULL OR receipt.operation_id IS DISTINCT FROM anchor
        OR receipt.response::text IS DISTINCT FROM expected::text THEN
        RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='Fixed document receipt differs from facts',
            CONSTRAINT='ck_document_revision_receipt';
    END IF;
END; $$;
"""

_CHECK = """
CREATE FUNCTION coinpup_check_document_revision_receipt() RETURNS trigger
LANGUAGE plpgsql SET search_path=pg_catalog AS $$
BEGIN
    IF TG_TABLE_NAME='command_receipts' THEN
        PERFORM public.coinpup_validate_document_revision_receipt(NEW.ledger_id,NEW.key);
    ELSE
        PERFORM public.coinpup_validate_document_revision_receipt(
            NEW.ledger_id,NEW.idempotency_key);
    END IF;
    RETURN NULL;
END; $$;
"""

_SEAL = """
CREATE FUNCTION coinpup_reject_sealed_document_member() RETURNS trigger
LANGUAGE plpgsql SET search_path=pg_catalog AS $$
BEGIN
    -- The preceding line/member guard already holds the writes lock at READ COMMITTED.
    IF EXISTS (
        SELECT 1 FROM public.business_document_revisions h JOIN public.command_receipts r
            ON (r.ledger_id,r.key)=(h.ledger_id,h.idempotency_key)
        WHERE (h.id,h.ledger_id,h.document_id)=(NEW.revision_id,NEW.ledger_id,NEW.document_id)
            AND r.response_status=200 AND r.response->>'kind'='business_document_revision'
            AND r.response->>'revision_id'=h.id::text
    ) THEN
        RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='Document revision members are sealed',
            CONSTRAINT='ck_document_revision_sealed';
    END IF;
    RETURN NEW;
END; $$;
"""

_TRIGGERS = {
    "business_document_revisions": (
        "CREATE CONSTRAINT TRIGGER zz_tr_document_revision_receipt AFTER INSERT ON "
        "business_document_revisions DEFERRABLE INITIALLY DEFERRED FOR EACH ROW "
        "EXECUTE FUNCTION coinpup_check_document_revision_receipt()"
    ),
    "command_receipts": (
        "CREATE CONSTRAINT TRIGGER tr_command_document_revision AFTER INSERT ON command_receipts "
        "DEFERRABLE INITIALLY DEFERRED FOR EACH ROW "
        "WHEN (NEW.response->>'kind'='business_document_revision') "
        "EXECUTE FUNCTION coinpup_check_document_revision_receipt()"
    ),
}
for table in ("business_document_revision_lines", "business_document_accruals"):
    _TRIGGERS[table] = (
        f"CREATE TRIGGER zz_tr_document_sealed_before_insert BEFORE INSERT ON {table} "
        "FOR EACH ROW EXECUTE FUNCTION coinpup_reject_sealed_document_member()"
    )


def upgrade():
    op.execute("SELECT pg_advisory_xact_lock(18945999704708432)")
    for definition in (_PROOF, _CHECK, _SEAL, *_TRIGGERS.values()):
        op.execute(definition)


def downgrade():
    op.execute("SELECT pg_advisory_xact_lock(18945999704708432)")
    op.execute("LOCK TABLE public.business_document_revisions IN ACCESS EXCLUSIVE MODE")
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM public.business_document_revisions)
                OR EXISTS (SELECT 1 FROM public.command_receipts
                    WHERE response->>'kind'='business_document_revision') THEN
                RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='Document receipts prevent downgrade',
                    CONSTRAINT='ck_document_revision_seal_downgrade';
            END IF;
        END; $$;
    """)
    op.execute("DROP TRIGGER tr_command_document_revision ON command_receipts")
    op.execute("DROP TRIGGER zz_tr_document_revision_receipt ON business_document_revisions")
    for table in ("business_document_revision_lines", "business_document_accruals"):
        op.execute(f"DROP TRIGGER zz_tr_document_sealed_before_insert ON {table}")
    op.execute("DROP FUNCTION coinpup_reject_sealed_document_member()")
    op.execute("DROP FUNCTION coinpup_check_document_revision_receipt()")
    op.execute("DROP FUNCTION coinpup_validate_document_revision_receipt(uuid,text)")
