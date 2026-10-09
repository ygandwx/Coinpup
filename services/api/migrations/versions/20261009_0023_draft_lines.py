"""Scoped draft lines retain exact inputs and validate prices and document shape."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "20261009_0023"
down_revision = "20261009_0022"
branch_labels = None
depends_on = None

_FIELDS = (
    ("line_no", sa.Integer()),
    ("description", sa.String(2000)),
    ("asset_id", sa.String(200)),
    ("quantity", sa.String(39)),
    ("unit_price", sa.String(40)),
    ("discount_amount", sa.String(40)),
    ("tax_rate_percent", sa.String(39)),
    ("category_id", sa.Uuid()),
    ("category_kind", sa.String(7)),
    ("project_id", sa.Uuid()),
    ("recognition_date", sa.Date()),
    ("category_snapshot", postgresql.JSONB()),
    ("project_snapshot", postgresql.JSONB()),
    ("net_amount", sa.Numeric(38, 18)),
    ("tax_amount", sa.Numeric(38, 18)),
    ("total_amount", sa.Numeric(38, 18)),
)
_PROFILE = (
    "("
    + " AND ".join(f"{name} IS NULL" for name, _ in _FIELDS)
    + ") OR ("
    + " AND ".join(
        f"{name} IS NOT NULL"
        for name, _ in _FIELDS
        if name not in ("project_id", "project_snapshot")
    )
    + " AND line_no > 0 AND btrim(description) <> '' "
    "AND category_kind IN ('income', 'expense') AND jsonb_typeof(category_snapshot) = 'object' "
    "AND ((project_id IS NULL AND project_snapshot IS NULL) OR (project_id IS NOT NULL "
    "AND project_snapshot IS NOT NULL AND jsonb_typeof(project_snapshot) = 'object')))"
)
_GUARD = """
    CREATE FUNCTION coinpup_guard_draft_line() RETURNS trigger
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
        IF NEW.quantity !~ '^(0|[1-9][0-9]{0,19})(\\.[0-9]{1,18})?$'
           OR NEW.tax_rate_percent !~ '^(0|[1-9][0-9]{0,19})(\\.[0-9]{1,18})?$'
           OR NEW.unit_price !~ '^-?(0|[1-9][0-9]{0,19})(\\.[0-9]{1,18})?$'
           OR NEW.discount_amount !~ '^-?(0|[1-9][0-9]{0,19})(\\.[0-9]{1,18})?$'
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
_DOCUMENT_SHAPE = """
    CREATE FUNCTION coinpup_validate_draft_lines() RETURNS trigger
    LANGUAGE plpgsql SET search_path = pg_catalog AS $$
    DECLARE target_document uuid; target_ledger uuid; header record; summary record;
    BEGIN
        IF TG_TABLE_NAME = 'business_documents' THEN target_document := NEW.id;
        ELSE target_document := NEW.document_id; END IF;
        target_ledger := NEW.ledger_id;
        SELECT * INTO header FROM public.business_documents d
            WHERE d.id = target_document AND d.ledger_id = target_ledger;
        IF NOT FOUND THEN RETURN NULL; END IF;
        SELECT count(*) AS count, coalesce(sum(l.total_amount), 0) AS total,
            bool_or(l.quantity IS NULL OR l.asset_id IS DISTINCT FROM header.asset_id
                OR l.category_kind IS DISTINCT FROM
                    CASE header.document_kind WHEN 'invoice' THEN 'income' ELSE 'expense' END
            ) AS invalid INTO summary
            FROM public.business_document_lines l
            WHERE l.document_id = target_document AND l.ledger_id = target_ledger
                AND NOT l.archived;
        IF header.state = 'draft' AND (summary.count > 200 OR summary.invalid
            OR summary.total >= 100000000000000000000) THEN
            RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'Inconsistent draft document lines',
                CONSTRAINT = 'ck_business_document_lines_shape';
        END IF;
        RETURN NULL;
    END; $$
"""


def upgrade():
    op.execute("SELECT pg_advisory_xact_lock(18945999704708432)")
    for name, type_ in _FIELDS:
        op.add_column("business_document_lines", sa.Column(name, type_, nullable=True))
    op.create_check_constraint(
        "ck_business_document_lines_profile", "business_document_lines", _PROFILE
    )
    op.create_unique_constraint(
        "uq_business_document_lines_position",
        "business_document_lines",
        ["document_id", "ledger_id", "line_no"],
    )
    for suffix, target, local, remote in (
        ("asset", "assets", ["asset_id"], ["asset_id"]),
        (
            "category",
            "categories",
            ["category_id", "ledger_id", "category_kind"],
            ["id", "ledger_id", "kind"],
        ),
        ("project", "business_projects", ["project_id", "ledger_id"], ["id", "ledger_id"]),
    ):
        op.create_foreign_key(
            f"fk_business_document_lines_{suffix}",
            "business_document_lines",
            target,
            local,
            remote,
            ondelete="RESTRICT",
        )
    op.execute(_GUARD)
    op.execute("DROP TRIGGER tr_business_document_lines_identity ON business_document_lines")
    op.execute(
        "CREATE TRIGGER tr_business_document_lines_identity BEFORE INSERT OR UPDATE OR DELETE "
        "ON business_document_lines FOR EACH ROW EXECUTE FUNCTION coinpup_guard_draft_line()"
    )
    op.execute(_DOCUMENT_SHAPE)
    for table in ("business_documents", "business_document_lines"):
        op.execute(
            f"CREATE CONSTRAINT TRIGGER tr_{table}_draft_shape AFTER INSERT OR UPDATE "
            f"ON {table} DEFERRABLE INITIALLY DEFERRED FOR EACH ROW "
            "EXECUTE FUNCTION coinpup_validate_draft_lines()"
        )


def downgrade():
    op.execute("SELECT pg_advisory_xact_lock(18945999704708432)")
    op.execute("LOCK TABLE business_documents, business_document_lines IN ACCESS EXCLUSIVE MODE")
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM public.business_document_lines WHERE quantity IS NOT NULL) THEN
                RAISE EXCEPTION USING ERRCODE = '23514',
                    MESSAGE = 'Draft line history prevents downgrade',
                    CONSTRAINT = 'ck_draft_line_downgrade';
            END IF;
        END; $$
    """)
    for table in ("business_documents", "business_document_lines"):
        op.execute(f"DROP TRIGGER tr_{table}_draft_shape ON {table}")
    op.execute("DROP FUNCTION coinpup_validate_draft_lines()")
    op.execute("DROP TRIGGER tr_business_document_lines_identity ON business_document_lines")
    op.execute(
        "CREATE TRIGGER tr_business_document_lines_identity BEFORE INSERT OR UPDATE OR DELETE "
        "ON business_document_lines FOR EACH ROW "
        "EXECUTE FUNCTION coinpup_guard_business_reference()"
    )
    op.execute("DROP FUNCTION coinpup_guard_draft_line()")
    for suffix in ("project", "category", "asset"):
        op.drop_constraint(
            f"fk_business_document_lines_{suffix}", "business_document_lines", type_="foreignkey"
        )
    op.drop_constraint(
        "uq_business_document_lines_position", "business_document_lines", type_="unique"
    )
    op.drop_constraint(
        "ck_business_document_lines_profile", "business_document_lines", type_="check"
    )
    for name, _ in reversed(_FIELDS):
        op.drop_column("business_document_lines", name)
