"""Add native mainnet units and custom token identities without re-keying assets."""

import sqlalchemy as sa
from alembic import op

revision = "20261127_0115"
down_revision = "20261127_0114"
branch_labels = None
depends_on = None

_OLD_IDENTITY = """
((kind = 'fiat' AND code ~ '^[A-Z]{3}$' AND code NOT IN ('BTC', 'ETH', 'XMR')
  AND (code NOT IN ('USD', 'GBP', 'EUR', 'HKD', 'CNY') OR scale = 2)
  AND network IS NULL AND token_reference IS NULL AND asset_id = code) OR
(kind = 'native' AND token_reference IS NULL AND network IS NOT NULL
 AND asset_id = code
 AND ((code = 'BTC' AND network = 'bitcoin' AND scale = 8)
       OR (code = 'ETH' AND network = 'ethereum' AND scale = 18)
       OR (code = 'XMR' AND network = 'monero' AND scale = 12))) OR
(kind = 'token' AND code IN ('USDT', 'USDC')
 AND network IS NOT NULL AND network ~ '^[a-z][a-z0-9_-]{0,63}$'
 AND token_reference IS NOT NULL
 AND token_reference ~ '^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$'
 AND asset_id = 'token:' || network || ':' || token_reference))
"""

_IDENTITY = """
((kind = 'fiat' AND code ~ '^[A-Z]{3}$' AND code NOT IN ('BTC', 'ETH', 'XMR')
  AND (code NOT IN ('USD', 'GBP', 'EUR', 'HKD', 'CNY') OR scale = 2)
  AND network IS NULL AND token_reference IS NULL AND asset_id = code) OR
(kind = 'native' AND token_reference IS NULL AND network IS NOT NULL
 AND asset_id = code
 AND ((code = 'BTC' AND network = 'bitcoin' AND scale = 8)
       OR (code = 'ETH' AND network = 'ethereum' AND scale = 18)
       OR (code = 'XMR' AND network = 'monero' AND scale = 12))) OR
(kind = 'native' AND token_reference IS NULL AND network IS NOT NULL
 AND asset_id = 'native:' || network || ':' || code
 AND ((code = 'SOL' AND network = 'solana' AND scale = 9)
       OR (code = 'BNB' AND network = 'bsc' AND scale = 18)
       OR (code = 'TRX' AND network = 'tron' AND scale = 6)
       OR (code = 'XRP' AND network = 'xrpl' AND scale = 6)
       OR (code = 'DOGE' AND network = 'dogecoin' AND scale = 8)
       OR (code = 'LTC' AND network = 'litecoin' AND scale = 8)
       OR (code = 'ADA' AND network = 'cardano' AND scale = 6)
       OR (code = 'TON' AND network = 'ton' AND scale = 9)
       OR (code = 'AVAX' AND network = 'avalanche' AND scale = 18)
       OR (code = 'DOT' AND network = 'polkadot' AND scale = 10)
       OR (code = 'POL' AND network = 'polygon' AND scale = 18))) OR
(kind = 'token' AND code ~ '^[A-Z0-9]{2,10}$'
 AND network IS NOT NULL AND network ~ '^[a-z][a-z0-9_-]{0,63}$'
 AND token_reference IS NOT NULL
 AND token_reference ~ '^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$'
 AND asset_id = 'token:' || network || ':' || token_reference))
"""


_EVM_IDENTITY_GUARD = """
CREATE FUNCTION public.coinpup_guard_new_evm_token_identity() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog AS $$
BEGIN
    IF NEW.kind = 'token'
       AND NEW.network IN ('ethereum', 'bsc', 'polygon', 'arbitrum',
                           'optimism', 'base', 'avalanche')
       AND NEW.token_reference ~ '^0[xX][0-9A-Fa-f]{40}$' THEN
        -- Every new variant must see the rows committed by the preceding writer.
        -- Existing identifiers and historically imported case variants remain readable.
        IF current_setting('transaction_isolation') <> 'read committed' THEN
            RAISE EXCEPTION USING ERRCODE = '23514',
                MESSAGE = 'New EVM assets require read committed isolation',
                CONSTRAINT = 'ck_assets_evm_write_isolation';
        END IF;
        PERFORM pg_advisory_xact_lock(18945999704708432);
        IF EXISTS (
            SELECT 1 FROM public.assets asset
            WHERE asset.kind = 'token' AND asset.network = NEW.network
                AND lower(asset.token_reference) = lower(NEW.token_reference)
                AND asset.asset_id <> NEW.asset_id
        ) THEN
            RAISE EXCEPTION USING ERRCODE = '23505',
                MESSAGE = 'A token with this network and contract already exists',
                CONSTRAINT = 'uq_assets_evm_contract';
        END IF;
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER tr_assets_evm_contract BEFORE INSERT ON public.assets
FOR EACH ROW EXECUTE FUNCTION public.coinpup_guard_new_evm_token_identity();
"""


def _replace_identity(condition):
    op.drop_constraint("ck_assets_identity", "assets", type_="check")
    op.create_check_constraint("ck_assets_identity", "assets", condition)


def upgrade():
    op.execute("SELECT pg_advisory_xact_lock(18945999704708432)")
    op.alter_column("assets", "code", existing_type=sa.String(8), type_=sa.String(10))
    _replace_identity(_IDENTITY)
    op.execute(_EVM_IDENTITY_GUARD)


def downgrade():
    op.execute("SELECT pg_advisory_xact_lock(18945999704708432)")
    op.execute(f"""
        LOCK TABLE public.assets IN ACCESS EXCLUSIVE MODE;
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM public.assets WHERE ({_OLD_IDENTITY}) IS NOT TRUE)
                OR EXISTS (SELECT 1 FROM public.change_log
                           WHERE entity_type = 'assets'
                               AND entity_id LIKE 'native:%') THEN
                RAISE EXCEPTION USING ERRCODE = '23514',
                    MESSAGE = 'Expanded asset history prevents downgrade',
                    CONSTRAINT = 'ck_assets_catalog_downgrade';
            END IF;
        END; $$;
    """)
    op.execute("DROP TRIGGER tr_assets_evm_contract ON public.assets")
    op.execute("DROP FUNCTION public.coinpup_guard_new_evm_token_identity()")
    _replace_identity(_OLD_IDENTITY)
    op.alter_column("assets", "code", existing_type=sa.String(10), type_=sa.String(8))
