"""Separate money accounts from immutable system control identities."""

import sqlalchemy as sa
from alembic import op

revision = "20261009_0015"
down_revision = "20261008_0014"
branch_labels = None
depends_on = None

_IDENTITY = (
    "(account_class = 'money' AND system_key IS NULL AND kind IS NOT NULL) OR "
    "(kind IS NULL AND system_key IS NOT NULL AND ("
    "(account_class = 'receivable' AND system_key = 'receivable.customer') OR "
    "(account_class = 'payable' AND system_key = 'payable.supplier') OR "
    "(account_class = 'intercompany' AND system_key IN "
    "('intercompany.receivable', 'intercompany.payable')) OR "
    "(account_class = 'advance' AND system_key IN ('advance.received', 'advance.paid'))))"
)


def upgrade():
    op.add_column(
        "accounts",
        sa.Column("account_class", sa.String(16), nullable=False, server_default="money"),
    )
    op.add_column("accounts", sa.Column("system_key", sa.String(40), nullable=True))
    op.alter_column("accounts", "kind", existing_type=sa.String(16), nullable=True)
    op.create_unique_constraint(
        "uq_accounts_ledger_system_key", "accounts", ["ledger_id", "system_key"]
    )
    op.create_check_constraint("ck_accounts_class_identity", "accounts", _IDENTITY)
    op.execute("""
        CREATE FUNCTION coinpup_guard_account_class() RETURNS trigger
        LANGUAGE plpgsql SET search_path = pg_catalog AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                IF OLD.account_class = 'money' THEN RETURN OLD; END IF;
            ELSIF NEW.account_class IS NOT DISTINCT FROM OLD.account_class
              AND NEW.system_key IS NOT DISTINCT FROM OLD.system_key
              AND (OLD.account_class = 'money' OR
                   (NEW.id = OLD.id AND NEW.ledger_id = OLD.ledger_id)) THEN
                RETURN NEW;
            END IF;
            RAISE EXCEPTION USING ERRCODE = '23514',
                MESSAGE = 'Account class and system identity are immutable',
                CONSTRAINT = 'ck_accounts_class_immutable';
        END; $$
    """)
    op.execute(
        "CREATE TRIGGER tr_accounts_class_guard BEFORE UPDATE OR DELETE ON accounts "
        "FOR EACH ROW EXECUTE FUNCTION coinpup_guard_account_class()"
    )


def downgrade():
    op.execute("SELECT pg_advisory_xact_lock(18945999704708432)")
    op.execute("LOCK TABLE accounts IN ACCESS EXCLUSIVE MODE")
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM public.accounts WHERE account_class <> 'money'
                       OR system_key IS NOT NULL) THEN
                RAISE EXCEPTION USING ERRCODE = '23514',
                    MESSAGE = 'System account history prevents downgrade',
                    CONSTRAINT = 'ck_accounts_class_downgrade';
            END IF;
        END; $$
    """)
    op.execute("DROP TRIGGER tr_accounts_class_guard ON accounts")
    op.execute("DROP FUNCTION coinpup_guard_account_class()")
    op.drop_constraint("ck_accounts_class_identity", "accounts", type_="check")
    op.drop_constraint("uq_accounts_ledger_system_key", "accounts", type_="unique")
    op.alter_column("accounts", "kind", existing_type=sa.String(16), nullable=False)
    op.drop_column("accounts", "system_key")
    op.drop_column("accounts", "account_class")
