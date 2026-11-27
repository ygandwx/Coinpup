"""Store crypto platform and per-asset details in their existing owned records."""

import sqlalchemy as sa
from alembic import op

revision = "20261127_0117"
down_revision = "20261127_0116"
branch_labels = None
depends_on = None

_PLATFORM_COLUMNS = (
    ("crypto_platform_key", 72),
    ("crypto_platform_id", 40),
    ("crypto_platform_name", 80),
    ("crypto_platform_uid", 160),
    ("crypto_platform_email", 254),
    ("crypto_platform_note", 1000),
)
_ASSET_COLUMNS = (
    ("crypto_network", 64),
    ("crypto_address", 128),
    ("crypto_memo", 256),
    ("crypto_label", 160),
)
_PLATFORM_STRUCTURE = """
    (crypto_platform_key IS NULL AND crypto_platform_id IS NULL
     AND crypto_platform_name IS NULL AND crypto_platform_uid IS NULL
     AND crypto_platform_email IS NULL AND crypto_platform_note IS NULL)
    OR (crypto_platform_key IS NOT NULL AND crypto_profile_used AND account_class = 'money'
        AND (kind IS DISTINCT FROM 'crypto' OR custody_mode IS NOT NULL)
        AND ((crypto_platform_id IS NOT NULL AND crypto_platform_name IS NULL
              AND crypto_platform_key = 'catalog:' || crypto_platform_id)
             OR (crypto_platform_id IS NULL AND crypto_platform_name IS NOT NULL
                 AND btrim(crypto_platform_name) <> ''
                 AND crypto_platform_key ~ '^custom:[0-9a-f]{64}$')))
"""
_ASSET_NETWORK = """
    (crypto_network IS NOT NULL AND crypto_network ~ '^[a-z][a-z0-9_-]{0,63}$')
    OR (crypto_network IS NULL AND crypto_address IS NULL AND crypto_memo IS NULL)
"""


def upgrade():
    op.execute("SELECT pg_advisory_xact_lock(18945999704708432)")
    for column, size in _PLATFORM_COLUMNS:
        op.add_column("accounts", sa.Column(column, sa.String(size), nullable=True))
    op.add_column(
        "accounts",
        sa.Column(
            "crypto_profile_used", sa.Boolean(), nullable=False, server_default=sa.text("false")
        ),
    )
    for column, size in _ASSET_COLUMNS:
        op.add_column("account_assets", sa.Column(column, sa.String(size), nullable=True))
    op.create_check_constraint("ck_accounts_crypto_platform", "accounts", _PLATFORM_STRUCTURE)
    op.create_check_constraint("ck_account_assets_crypto_network", "account_assets", _ASSET_NETWORK)
    op.create_index(
        "uq_accounts_crypto_platform_ledger",
        "accounts",
        ["ledger_id", "crypto_platform_key"],
        unique=True,
        postgresql_where=sa.text("kind = 'crypto' AND crypto_platform_key IS NOT NULL"),
    )
    op.execute("""
        CREATE FUNCTION public.coinpup_guard_crypto_profile_history() RETURNS trigger
        LANGUAGE plpgsql SET search_path = pg_catalog AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                IF OLD.crypto_profile_used THEN
                    RAISE EXCEPTION USING ERRCODE = '23514',
                        MESSAGE = 'Crypto account profile history exists',
                        CONSTRAINT = 'ck_accounts_crypto_profile_history';
                END IF;
                RETURN OLD;
            END IF;
            IF OLD.crypto_profile_used AND NEW.crypto_profile_used IS DISTINCT FROM true THEN
                RAISE EXCEPTION USING ERRCODE = '23514',
                    MESSAGE = 'Crypto account profile history exists',
                    CONSTRAINT = 'ck_accounts_crypto_profile_history';
            END IF;
            RETURN NEW;
        END;
        $$;
        CREATE TRIGGER tr_accounts_crypto_profile_history
            BEFORE UPDATE OF crypto_profile_used OR DELETE ON public.accounts
            FOR EACH ROW EXECUTE FUNCTION public.coinpup_guard_crypto_profile_history();

        CREATE FUNCTION public.coinpup_guard_crypto_asset_profile_history() RETURNS trigger
        LANGUAGE plpgsql SET search_path = pg_catalog AS $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM public.accounts account
                WHERE (account.id, account.ledger_id) = (NEW.account_id, NEW.ledger_id)
                  AND account.crypto_profile_used
            ) THEN
                RAISE EXCEPTION USING ERRCODE = '23514',
                    MESSAGE = 'Crypto asset details require account profile history',
                    CONSTRAINT = 'ck_account_assets_crypto_profile_history';
            END IF;
            RETURN NEW;
        END;
        $$;
        CREATE TRIGGER tr_account_assets_crypto_profile_history
            BEFORE INSERT OR UPDATE OF crypto_network, crypto_address, crypto_memo, crypto_label
            ON public.account_assets FOR EACH ROW
            WHEN (num_nonnulls(
                NEW.crypto_network, NEW.crypto_address, NEW.crypto_memo, NEW.crypto_label
            ) > 0)
            EXECUTE FUNCTION public.coinpup_guard_crypto_asset_profile_history();
    """)


def downgrade():
    op.execute("SELECT pg_advisory_xact_lock(18945999704708432)")
    op.execute("""
        LOCK TABLE public.accounts, public.account_assets IN ACCESS EXCLUSIVE MODE;
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM public.accounts WHERE crypto_profile_used)
                OR EXISTS (SELECT 1 FROM public.account_assets
                           WHERE crypto_network IS NOT NULL OR crypto_address IS NOT NULL
                              OR crypto_memo IS NOT NULL OR crypto_label IS NOT NULL)
            THEN
                RAISE EXCEPTION USING ERRCODE = '23514',
                    MESSAGE = 'Crypto account profile history prevents downgrade',
                    CONSTRAINT = 'ck_crypto_account_profile_downgrade';
            END IF;
        END; $$;
        DROP TRIGGER tr_account_assets_crypto_profile_history ON public.account_assets;
        DROP FUNCTION public.coinpup_guard_crypto_asset_profile_history();
        DROP TRIGGER tr_accounts_crypto_profile_history ON public.accounts;
        DROP FUNCTION public.coinpup_guard_crypto_profile_history();
    """)
    op.drop_index("uq_accounts_crypto_platform_ledger", table_name="accounts")
    op.drop_constraint("ck_account_assets_crypto_network", "account_assets", type_="check")
    op.drop_constraint("ck_accounts_crypto_platform", "accounts", type_="check")
    for column, _ in reversed(_ASSET_COLUMNS):
        op.drop_column("account_assets", column)
    op.drop_column("accounts", "crypto_profile_used")
    for column, _ in reversed(_PLATFORM_COLUMNS):
        op.drop_column("accounts", column)
