-- Frozen issuer identities from the checked 2026-11-27 registry.
CREATE FUNCTION public.coinpup_custody_family(identifier text) RETURNS text
LANGUAGE sql STABLE SET search_path = pg_catalog AS $$
    SELECT asset.code FROM public.assets asset
    WHERE asset.asset_id = identifier AND (
        asset.kind IN ('native', 'custodial') OR (asset.kind = 'token' AND EXISTS (
            SELECT 1 FROM (VALUES
        ('USDT', 'ethereum', '0xdac17f958d2ee523a2206206994597c13d831ec7', 6)
        , ('USDT', 'tron', 'TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t', 6)
        , ('USDT', 'solana', 'Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB', 6)
        , ('USDT', 'ton', 'EQCxE6mUtQJKFnGfaROTKOt1lZbDiiX1kCixRv7Nw2Id_sDs', 6)
        , ('USDT', 'avalanche', '0x9702230a8ea53601f5cd2dc00fdbc13d4df4a8c7', 6)
        , ('USDC', 'ethereum', '0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48', 6)
        , ('USDC', 'solana', 'EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v', 6)
        , ('USDC', 'polygon', '0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359', 6)
        , ('USDC', 'arbitrum', '0xaf88d065e77c8cC2239327C5EDb3A432268e5831', 6)
        , ('USDC', 'optimism', '0x0b2C639c533813f4Aa9D7837CAf62653d097Ff85', 6)
        , ('USDC', 'base', '0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913', 6)
        , ('USDC', 'avalanche', '0xB97EF9Ef8734C71904D8002F8b6Bc66Dd9c48a6E', 6)
        , ('DAI', 'ethereum', '0x6B175474E89094C44Da98b954EedeAC495271d0F', 18)
        , ('DAI', 'arbitrum', '0xDA10009cBd5D07dd0CeCc66161FC93D7c9000da1', 18)
        , ('DAI', 'optimism', '0xDA10009cBd5D07dd0CeCc66161FC93D7c9000da1', 18)
        , ('FDUSD', 'ethereum', '0xc5f0f7b66764F6ec8C8Dff7BA683102295E16409', 18)
        , ('FDUSD', 'bsc', '0xc5f0f7b66764F6ec8C8Dff7BA683102295E16409', 18)
        , ('FDUSD', 'solana', '9zNQRsGLjNKwCUU5Gq5LR8beUCPzQMVMqKAi3SSZh54u', 6)
        , ('FDUSD', 'arbitrum', '0x93C9932E4afa59201F0B5E63f7d816516F1669fE', 18)
        , ('FDUSD', 'ton', 'EQD0Evpk4timFOHmy4Sv3l_KEUXlM-dN1_KhroTCfB2wkO89', 6)
            ) AS issuer(code, network, reference, scale)
            WHERE (issuer.code, issuer.network, issuer.scale) =
                  (asset.code, asset.network, asset.scale)
              AND CASE WHEN issuer.network IN
                  ('ethereum', 'bsc', 'polygon', 'arbitrum', 'optimism', 'base', 'avalanche')
                  THEN lower(issuer.reference) = lower(asset.token_reference)
                  ELSE issuer.reference = asset.token_reference END
        ))
    );
$$;

CREATE FUNCTION public.coinpup_custody_link_allowed(
    account_class text, custody text, asset_kind text
) RETURNS boolean
LANGUAGE sql IMMUTABLE SET search_path = pg_catalog AS $$
    SELECT account_class <> 'money' OR CASE custody
        WHEN 'custodial' THEN asset_kind = 'custodial'
        WHEN 'onchain' THEN asset_kind IN ('native', 'token')
        ELSE asset_kind <> 'custodial' END;
$$;

CREATE FUNCTION public.coinpup_validate_custody_link() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog AS $$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM public.accounts account, public.assets asset
        WHERE account.id = NEW.account_id AND asset.asset_id = NEW.asset_id
          AND NOT public.coinpup_custody_link_allowed(
              account.account_class, account.custody_mode, asset.kind)
    ) THEN
        RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'Incompatible custody and asset',
            CONSTRAINT = 'ck_account_assets_custody';
    END IF;
    RETURN NEW;
END; $$;

CREATE FUNCTION public.coinpup_validate_custody_mode() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog AS $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM public.account_assets link
        JOIN public.assets asset USING (asset_id)
        WHERE link.account_id = NEW.id AND NOT public.coinpup_custody_link_allowed(
            NEW.account_class, NEW.custody_mode, asset.kind)
    ) THEN
        RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'Incompatible retained custody asset',
            CONSTRAINT = 'ck_account_assets_custody';
    END IF;
    RETURN NEW;
END; $$;

CREATE TRIGGER tr_account_assets_custody BEFORE INSERT OR UPDATE ON public.account_assets
FOR EACH ROW EXECUTE FUNCTION public.coinpup_validate_custody_link();

CREATE TRIGGER tr_accounts_custody BEFORE UPDATE OF kind, custody_mode ON public.accounts
FOR EACH ROW EXECUTE FUNCTION public.coinpup_validate_custody_mode();
