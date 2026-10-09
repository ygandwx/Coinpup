"""Versioned party profiles preserve existing scoped identities (ADR 0024)."""

import sqlalchemy as sa
from alembic import op

revision = "20261009_0019"
down_revision = "20261009_0018"
branch_labels = None
depends_on = None

_FIELDS = {
    "name": 160,
    "role": 8,
    "legal_name": 200,
    "email": 254,
    "phone": 64,
    "address": 1000,
    "tax_identifier": 128,
    "notes": 2000,
}
_PROFILE_CHECK = (
    "(name IS NULL AND role IS NULL AND legal_name IS NULL AND email IS NULL "
    "AND phone IS NULL AND address IS NULL AND tax_identifier IS NULL AND notes IS NULL) "
    "OR (name IS NOT NULL AND role IS NOT NULL AND btrim(name) <> '' "
    "AND role IN ('customer', 'supplier', 'both'))"
)
_GUARD_SQL = """
    CREATE FUNCTION coinpup_guard_party_profile() RETURNS trigger
    LANGUAGE plpgsql SET search_path = pg_catalog AS $$
    DECLARE mutable_fields text[] := ARRAY[
        'version', 'archived', 'updated_at', 'name', 'role', 'legal_name',
        'email', 'phone', 'address', 'tax_identifier', 'notes'
    ];
    BEGIN
        IF TG_OP = 'INSERT' THEN
            IF NEW.version = 1 AND NOT NEW.archived THEN RETURN NEW; END IF;
        ELSIF TG_OP = 'UPDATE' THEN
            IF NEW.version::bigint = OLD.version::bigint + 1
               AND (to_jsonb(NEW) - mutable_fields) IS NOT DISTINCT FROM
                   (to_jsonb(OLD) - mutable_fields)
               AND (OLD.name IS NULL OR NEW.name IS NOT NULL) THEN
                RETURN NEW;
            END IF;
        END IF;
        RAISE EXCEPTION USING ERRCODE = '23514',
            MESSAGE = 'Business reference identity or version is immutable',
            CONSTRAINT = 'ck_business_reference_identity';
    END; $$
"""


def upgrade():
    op.execute("SELECT pg_advisory_xact_lock(18945999704708432)")
    for name, length in _FIELDS.items():
        op.add_column("business_parties", sa.Column(name, sa.String(length), nullable=True))
    op.create_check_constraint("ck_business_parties_profile", "business_parties", _PROFILE_CHECK)
    op.execute(_GUARD_SQL)
    op.execute("DROP TRIGGER tr_business_parties_identity ON business_parties")
    op.execute(
        "CREATE TRIGGER tr_business_parties_identity BEFORE INSERT OR UPDATE OR DELETE "
        "ON business_parties FOR EACH ROW EXECUTE FUNCTION coinpup_guard_party_profile()"
    )


def downgrade():
    op.execute("SELECT pg_advisory_xact_lock(18945999704708432)")
    op.execute("LOCK TABLE business_parties IN ACCESS EXCLUSIVE MODE")
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM public.business_parties WHERE name IS NOT NULL)
               OR EXISTS (SELECT 1 FROM public.change_log WHERE entity_type = 'business_parties')
            THEN
                RAISE EXCEPTION USING ERRCODE = '23514',
                    MESSAGE = 'Party profile history prevents downgrade',
                    CONSTRAINT = 'ck_party_profile_downgrade';
            END IF;
        END; $$
    """)
    op.execute("DROP TRIGGER tr_business_parties_identity ON business_parties")
    op.execute(
        "CREATE TRIGGER tr_business_parties_identity BEFORE INSERT OR UPDATE OR DELETE "
        "ON business_parties FOR EACH ROW EXECUTE FUNCTION coinpup_guard_business_reference()"
    )
    op.execute("DROP FUNCTION coinpup_guard_party_profile()")
    op.drop_constraint("ck_business_parties_profile", "business_parties", type_="check")
    for name in reversed(_FIELDS):
        op.drop_column("business_parties", name)
