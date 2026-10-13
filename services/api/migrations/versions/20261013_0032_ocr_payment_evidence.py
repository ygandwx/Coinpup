"""Link OCR evidence to original payments without any new financial posting."""

import sqlalchemy as sa
from alembic import op

revision = "20261013_0032"
down_revision = "20261012_0031"
branch_labels = None
depends_on = None

# Exact frozen 0014 body: legacy envelopes retain their original validation semantics.
_OLD_CONSISTENCY_SQL = r"""
    CREATE FUNCTION coinpup_check_ocr_confirmation() RETURNS trigger
    LANGUAGE plpgsql SET search_path = pg_catalog AS $$
    DECLARE draft_key uuid; d record; c record;
    BEGIN
        IF TG_TABLE_NAME = 'ocr_drafts' THEN draft_key := NEW.id;
        ELSE draft_key := NEW.draft_id; END IF;
        SELECT * INTO d FROM public.ocr_drafts WHERE id = draft_key;
        SELECT * INTO c FROM public.ocr_confirmations WHERE draft_id = draft_key;
        IF d.id IS NULL OR (d.status = 'confirmed') IS DISTINCT FROM (c.id IS NOT NULL) THEN
            RAISE EXCEPTION USING ERRCODE = '23514',
                MESSAGE = 'OCR confirmation and draft must commit together',
                CONSTRAINT = 'ck_ocr_confirmation_consistency';
        END IF;
        IF c.id IS NULL THEN RETURN NULL; END IF;
        IF d.version <> c.draft_version::bigint + 1 OR d.fields IS DISTINCT FROM c.review
           OR d.source_key <> c.source_key OR NOT EXISTS (
            SELECT 1 FROM public.ocr_jobs j
            JOIN public.stored_files original ON original.id = j.file_id
                AND original.ledger_id = d.ledger_id
            JOIN public.stored_files target ON target.id = c.target_file_id
                AND target.ledger_id = c.target_ledger_id
            JOIN public.financial_operations o ON o.id = c.operation_id
                AND o.ledger_id = c.target_ledger_id AND o.version = c.operation_version
                AND o.status = 'active'
            JOIN public.operation_file_links evidence ON evidence.operation_id = o.id
                AND evidence.file_id = target.id AND evidence.ledger_id = c.target_ledger_id
                AND NOT evidence.archived
            WHERE j.id = d.job_id AND j.state = 'succeeded' AND original.id = c.source_file_id
                AND original.sha256 = target.sha256 AND original.byte_size = target.byte_size
                AND original.detected_media_type = target.detected_media_type
                AND NOT original.archived AND NOT target.archived
        ) THEN
            RAISE EXCEPTION USING ERRCODE = '23514',
                MESSAGE = 'OCR confirmation evidence or version is inconsistent',
                CONSTRAINT = 'ck_ocr_confirmation_consistency';
        END IF;
        RETURN NULL;
    END;
    $$
"""
_CONSISTENCY_SQL = r"""
    CREATE OR REPLACE FUNCTION coinpup_check_ocr_confirmation() RETURNS trigger
    LANGUAGE plpgsql SET search_path = pg_catalog AS $$
    DECLARE draft_key uuid; d record; c record;
    BEGIN
        IF TG_TABLE_NAME = 'ocr_drafts' THEN draft_key := NEW.id;
        ELSE draft_key := NEW.draft_id; END IF;
        SELECT * INTO d FROM public.ocr_drafts WHERE id = draft_key;
        SELECT * INTO c FROM public.ocr_confirmations WHERE draft_id = draft_key;
        IF d.id IS NULL OR (d.status = 'confirmed') IS DISTINCT FROM (c.id IS NOT NULL) THEN
            RAISE EXCEPTION USING ERRCODE = '23514',
                MESSAGE = 'OCR confirmation and draft must commit together',
                CONSTRAINT = 'ck_ocr_confirmation_consistency';
        END IF;
        IF c.id IS NULL THEN RETURN NULL; END IF;
        IF d.version <> c.draft_version::bigint + 1 OR d.fields IS DISTINCT FROM c.review
           OR d.source_key <> c.source_key OR NOT EXISTS (
            SELECT 1 FROM public.ocr_jobs j
            JOIN public.stored_files original ON original.id = j.file_id
                AND original.ledger_id = d.ledger_id
            JOIN public.stored_files target ON target.id = c.target_file_id
                AND target.ledger_id = c.target_ledger_id
            JOIN public.financial_operations o ON o.id = c.operation_id
                AND o.ledger_id = c.target_ledger_id AND o.version = c.operation_version
                AND o.status = 'active'
            JOIN public.operation_file_links evidence ON evidence.operation_id = o.id
                AND evidence.file_id = target.id AND evidence.ledger_id = c.target_ledger_id
                AND NOT evidence.archived
            WHERE j.id = d.job_id AND j.state = 'succeeded' AND original.id = c.source_file_id
                AND original.sha256 = target.sha256 AND original.byte_size = target.byte_size
                AND original.detected_media_type = target.detected_media_type
                AND NOT original.archived AND NOT target.archived
        ) THEN
            RAISE EXCEPTION USING ERRCODE = '23514',
                MESSAGE = 'OCR confirmation evidence or version is inconsistent',
                CONSTRAINT = 'ck_ocr_confirmation_consistency';
        END IF;

        IF c.action = 'link_payment' AND NOT EXISTS (
            SELECT 1 FROM public.business_payments p
            JOIN public.command_receipts r ON r.ledger_id = p.ledger_id
                AND r.key = p.idempotency_key AND r.operation_id = p.operation_id
                AND r.hash_version = 2 AND r.response_status = 201
            JOIN public.financial_operations o ON o.id = p.operation_id
                AND o.ledger_id = p.ledger_id AND o.created_by = p.created_by
                AND o.kind = 'payment'
            JOIN public.journals j ON j.id = p.journal_id AND j.operation_id = p.operation_id
                AND j.ledger_id = p.ledger_id AND j.journal_kind = 'posting'
            WHERE p.id = c.payment_id AND p.ledger_id = c.target_ledger_id
                AND p.created_by = c.created_by AND p.operation_id = c.operation_id
                AND r.response = public.coinpup_business_payment_response(p.id)
                AND c.response->>'draft_version' = (c.draft_version::bigint + 1)::text
                AND c.response->>'operation_version' = c.operation_version::text
                AND c.response = jsonb_build_object(
                    'intent_id', c.intent_id, 'draft_id', c.draft_id, 'ledger_id', c.ledger_id,
                    'draft_version', c.draft_version::bigint + 1, 'action', 'link_payment',
                    'target_ledger_id', c.target_ledger_id, 'target_file_id', c.target_file_id,
                    'payment', r.response, 'operation_version', c.operation_version)
        ) THEN
            RAISE EXCEPTION USING ERRCODE = '23514',
                MESSAGE = 'OCR payment confirmation is inconsistent',
                CONSTRAINT = 'ck_ocr_confirmation_payment';
        END IF;
        RETURN NULL;
    END;
    $$
"""


def upgrade():
    op.execute("SELECT pg_advisory_xact_lock(18945999704708432)")
    op.add_column("ocr_confirmations", sa.Column("payment_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        "fk_ocr_confirmations_payment",
        "ocr_confirmations",
        "business_payments",
        ["payment_id", "target_ledger_id"],
        ["id", "ledger_id"],
        ondelete="RESTRICT",
    )
    op.drop_constraint("ck_ocr_confirmations_action", "ocr_confirmations", type_="check")
    op.create_check_constraint(
        "ck_ocr_confirmations_action",
        "ocr_confirmations",
        "action IN ('create', 'link', 'link_payment')",
    )
    op.create_check_constraint(
        "ck_ocr_confirmations_payment",
        "ocr_confirmations",
        "(action = 'link_payment') = (payment_id IS NOT NULL)",
    )
    op.execute(_CONSISTENCY_SQL)


def downgrade():
    op.execute("SELECT pg_advisory_xact_lock(18945999704708432)")
    op.execute("LOCK TABLE ocr_confirmations, ocr_drafts IN ACCESS EXCLUSIVE MODE")
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM ocr_confirmations
                WHERE action = 'link_payment' OR payment_id IS NOT NULL) THEN
                RAISE EXCEPTION USING ERRCODE = '23514',
                    MESSAGE = 'OCR payment confirmation history prevents downgrade',
                    CONSTRAINT = 'ck_ocr_payment_confirmation_downgrade';
            END IF;
        END $$
    """)
    op.execute(_OLD_CONSISTENCY_SQL.replace("CREATE FUNCTION", "CREATE OR REPLACE FUNCTION"))
    op.drop_constraint("ck_ocr_confirmations_payment", "ocr_confirmations", type_="check")
    op.drop_constraint("ck_ocr_confirmations_action", "ocr_confirmations", type_="check")
    op.create_check_constraint(
        "ck_ocr_confirmations_action",
        "ocr_confirmations",
        "action IN ('create', 'link')",
    )
    op.drop_constraint("fk_ocr_confirmations_payment", "ocr_confirmations", type_="foreignkey")
    op.drop_column("ocr_confirmations", "payment_id")
