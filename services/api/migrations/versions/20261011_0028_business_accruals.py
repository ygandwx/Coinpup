"""Activate complete business accruals without changing legacy posting history."""

import sqlalchemy as sa
from alembic import op

revision = "20261011_0028"
down_revision = "20261011_0027"
branch_labels = None
depends_on = None

_OLD_KINDS = "kind IN ('opening', 'income', 'expense', 'transfer', 'exchange')"
_KINDS = (
    "kind IN ('opening', 'income', 'expense', 'transfer', 'exchange', 'invoice_accrual', "
    "'bill_accrual')"
)
_OLD_PROFILE = (
    "(document_kind IS NULL AND state IS NULL AND party_id IS NULL AND asset_id IS NULL "
    "AND issue_date IS NULL AND due_date IS NULL AND notes IS NULL AND issuer_snapshot IS"
    " NULL AND party_snapshot IS NULL) OR (document_kind IS NOT NULL AND document_kind IN"
    " ('invoice', 'bill') AND state IS NOT NULL AND state = 'draft' AND party_id IS NOT "
    "NULL AND asset_id IS NOT NULL AND issue_date IS NOT NULL AND issuer_snapshot IS NOT "
    "NULL AND jsonb_typeof(issuer_snapshot) = 'object' AND party_snapshot IS NOT NULL AND"
    " jsonb_typeof(party_snapshot) = 'object')"
)
_PROFILE = _OLD_PROFILE.replace("state = 'draft'", "state IN ('draft', 'confirmed')")

_OLD_HEADER = r"""

    CREATE OR REPLACE FUNCTION coinpup_guard_document_draft() RETURNS trigger
    LANGUAGE plpgsql SET search_path = pg_catalog AS $$
    DECLARE mutable_fields text[] := ARRAY[
        'version', 'archived', 'updated_at', 'document_kind', 'state', 'party_id',
        'asset_id', 'issue_date', 'due_date', 'notes', 'issuer_snapshot', 'party_snapshot'
    ];
    BEGIN
        IF TG_OP = 'INSERT' THEN
            IF NEW.version = 1 AND NOT NEW.archived THEN RETURN NEW; END IF;
        ELSIF TG_OP = 'UPDATE' THEN
            IF NEW.version::bigint = OLD.version::bigint + 1
               AND (to_jsonb(NEW) - mutable_fields) IS NOT DISTINCT FROM
                   (to_jsonb(OLD) - mutable_fields)
               AND ((OLD.document_kind IS NULL AND NEW.document_kind IS NULL)
                    OR (OLD.state = 'draft' AND NEW.state = 'draft'
                        AND NEW.document_kind IS NOT NULL)) THEN
                RETURN NEW;
            END IF;
        END IF;
        RAISE EXCEPTION USING ERRCODE = '23514',
            MESSAGE = 'Business reference identity or version is immutable',
            CONSTRAINT = 'ck_business_reference_identity';
    END; $$

"""

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

_OLD_DISPATCH = r"""

    CREATE OR REPLACE FUNCTION coinpup_validate_posting_shape(journal_identifier uuid)
    RETURNS void LANGUAGE plpgsql SET search_path = pg_catalog AS $$
    DECLARE header public.journals%ROWTYPE; operation public.financial_operations%ROWTYPE;
            common_result jsonb;
    BEGIN
        SELECT * INTO header FROM public.journals WHERE id = journal_identifier FOR UPDATE;
        IF NOT FOUND THEN
            RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'Journal is missing',
                CONSTRAINT = 'ck_journal_shape';
        END IF;
        SELECT * INTO operation FROM public.financial_operations WHERE id = header.operation_id;
        IF NOT FOUND OR operation.ledger_id <> header.ledger_id OR
           header.journal_kind <> 'posting' OR header.reverses_journal_id IS NOT NULL THEN
            RAISE EXCEPTION USING ERRCODE = '23514',
                MESSAGE = 'Posting journal identity is invalid', CONSTRAINT = 'ck_journal_shape';
        END IF;
        common_result := public.coinpup_validate_journal_common(journal_identifier);
        CASE operation.kind
            WHEN 'opening' THEN
                PERFORM public.coinpup_validate_shape_opening(journal_identifier, common_result);
            WHEN 'income' THEN
                PERFORM public.coinpup_validate_shape_income(journal_identifier, common_result);
            WHEN 'expense' THEN
                PERFORM public.coinpup_validate_shape_expense(journal_identifier, common_result);
            WHEN 'transfer' THEN
                PERFORM public.coinpup_validate_shape_transfer(journal_identifier, common_result);
            WHEN 'exchange' THEN
                PERFORM public.coinpup_validate_shape_exchange(journal_identifier, common_result);
            ELSE
                RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'Unknown operation kind',
                    CONSTRAINT = 'ck_journal_shape';
        END CASE;
    END;
    $$

"""

_HEADER = r"""

    CREATE OR REPLACE FUNCTION coinpup_guard_document_draft() RETURNS trigger
    LANGUAGE plpgsql SET search_path = pg_catalog AS $$
    DECLARE mutable_fields text[] := ARRAY[
        'version', 'archived', 'updated_at', 'document_kind', 'state', 'party_id',
        'asset_id', 'issue_date', 'due_date', 'notes', 'issuer_snapshot', 'party_snapshot'
    ];
    BEGIN
        IF TG_OP = 'INSERT' THEN
            IF NEW.version = 1 AND NOT NEW.archived
               AND NEW.state IS DISTINCT FROM 'confirmed' THEN RETURN NEW; END IF;
        ELSIF TG_OP = 'UPDATE' THEN
            IF OLD.state='draft' AND NEW.state='confirmed' AND NOT NEW.archived
               AND NEW.version::bigint=OLD.version::bigint+1
               AND (to_jsonb(NEW)-ARRAY['version','state','updated_at']) IS NOT DISTINCT FROM
                   (to_jsonb(OLD)-ARRAY['version','state','updated_at'])
               AND EXISTS (SELECT 1 FROM public.business_confirmations c
                   WHERE c.document_id=OLD.id AND c.ledger_id=OLD.ledger_id
                       AND c.source_version=OLD.version) THEN
                PERFORM pg_advisory_xact_lock(18945999704708432);
                IF EXISTS (SELECT 1 FROM public.ledger_periods p WHERE p.ledger_id=NEW.ledger_id
                    AND (NEW.issue_date<=p.closed_through OR EXISTS (
                        SELECT 1 FROM public.business_document_lines l
                        WHERE l.document_id=NEW.id AND NOT l.archived
                            AND l.recognition_date<=p.closed_through))) THEN
                    RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='The journal period is closed',
                        CONSTRAINT='ck_journal_period_closed';
                END IF;
                RETURN NEW;
            END IF;
            IF NEW.version::bigint = OLD.version::bigint + 1
               AND (to_jsonb(NEW) - mutable_fields) IS NOT DISTINCT FROM
                   (to_jsonb(OLD) - mutable_fields)
               AND ((OLD.document_kind IS NULL AND NEW.document_kind IS NULL)
                    OR (OLD.state = 'draft' AND NEW.state = 'draft'
                        AND NEW.document_kind IS NOT NULL)) THEN
                RETURN NEW;
            END IF;
        END IF;
        RAISE EXCEPTION USING ERRCODE = '23514',
            MESSAGE = 'Business reference identity or version is immutable',
            CONSTRAINT = 'ck_business_reference_identity';
    END; $$

"""

_LINE = r"""

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

_DISPATCH = r"""

    CREATE OR REPLACE FUNCTION coinpup_validate_posting_shape(journal_identifier uuid)
    RETURNS void LANGUAGE plpgsql SET search_path = pg_catalog AS $$
    DECLARE header public.journals%ROWTYPE; operation public.financial_operations%ROWTYPE;
            common_result jsonb;
    BEGIN
        SELECT * INTO header FROM public.journals WHERE id = journal_identifier FOR UPDATE;
        IF NOT FOUND THEN
            RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'Journal is missing',
                CONSTRAINT = 'ck_journal_shape';
        END IF;
        SELECT * INTO operation FROM public.financial_operations WHERE id = header.operation_id;
        IF NOT FOUND OR operation.ledger_id <> header.ledger_id OR
           header.journal_kind <> 'posting' OR header.reverses_journal_id IS NOT NULL THEN
            RAISE EXCEPTION USING ERRCODE = '23514',
                MESSAGE = 'Posting journal identity is invalid', CONSTRAINT = 'ck_journal_shape';
        END IF;
        common_result := public.coinpup_validate_journal_common(journal_identifier);
        CASE operation.kind
            WHEN 'opening' THEN
                PERFORM public.coinpup_validate_shape_opening(journal_identifier, common_result);
            WHEN 'income' THEN
                PERFORM public.coinpup_validate_shape_income(journal_identifier, common_result);
            WHEN 'expense' THEN
                PERFORM public.coinpup_validate_shape_expense(journal_identifier, common_result);
            WHEN 'transfer' THEN
                PERFORM public.coinpup_validate_shape_transfer(journal_identifier, common_result);
            WHEN 'exchange' THEN
                PERFORM public.coinpup_validate_shape_exchange(journal_identifier, common_result);
            WHEN 'invoice_accrual' THEN
                PERFORM public.coinpup_validate_shape_invoice_accrual(
                    journal_identifier, common_result);
            WHEN 'bill_accrual' THEN
                PERFORM public.coinpup_validate_shape_bill_accrual(
                    journal_identifier, common_result);
            ELSE
                RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'Unknown operation kind',
                    CONSTRAINT = 'ck_journal_shape';
        END CASE;
    END;
    $$

"""


def _checks(profile, kinds):
    op.drop_constraint("ck_business_documents_profile", "business_documents", type_="check")
    op.create_check_constraint("ck_business_documents_profile", "business_documents", profile)
    op.drop_constraint("ck_financial_operations_kind", "financial_operations", type_="check")
    op.create_check_constraint("ck_financial_operations_kind", "financial_operations", kinds)


def upgrade():
    op.execute("SELECT pg_advisory_xact_lock(18945999704708432)")
    op.alter_column("business_documents", "state", existing_type=sa.String(5), type_=sa.String(9))
    _checks(_PROFILE, _KINDS)
    for definition in (_HEADER, _LINE, _DISPATCH):
        op.execute(definition)


def downgrade():
    op.execute("SELECT pg_advisory_xact_lock(18945999704708432)")
    op.execute(
        "LOCK TABLE business_documents, financial_operations, business_confirmations, "
        "business_postings IN ACCESS EXCLUSIVE MODE"
    )
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM public.business_confirmations)
               OR EXISTS (SELECT 1 FROM public.business_postings)
               OR EXISTS (SELECT 1 FROM public.business_documents WHERE state='confirmed')
               OR EXISTS (SELECT 1 FROM public.financial_operations
                   WHERE kind IN ('invoice_accrual','bill_accrual'))
               OR EXISTS (SELECT 1 FROM public.change_log
                   WHERE entity_type IN ('business_confirmations','business_postings')) THEN
                RAISE EXCEPTION USING ERRCODE='23514',
                    MESSAGE='Business accrual history prevents downgrade',
                    CONSTRAINT='ck_business_accrual_downgrade';
            END IF;
        END; $$;
    """)
    for definition in (_OLD_HEADER, _OLD_LINE, _OLD_DISPATCH):
        op.execute(definition)
    _checks(_OLD_PROFILE, _OLD_KINDS)
    op.alter_column("business_documents", "state", existing_type=sa.String(9), type_=sa.String(5))
