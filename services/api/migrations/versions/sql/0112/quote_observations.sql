CREATE TABLE public.quote_observations (
    id uuid PRIMARY KEY,
    owner_id uuid NOT NULL,
    asset_id varchar(200) NOT NULL,
    display_asset_id varchar(200) NOT NULL,
    price varchar(39) NOT NULL,
    source varchar(128) NOT NULL,
    price_date date NOT NULL,
    observed_at timestamptz,
    fetched_at timestamptz NOT NULL,
    CONSTRAINT fk_quote_observation_owner FOREIGN KEY (owner_id)
        REFERENCES public.administrators(id) ON DELETE RESTRICT,
    CONSTRAINT fk_quote_observation_asset FOREIGN KEY (asset_id)
        REFERENCES public.assets(asset_id) ON DELETE RESTRICT,
    CONSTRAINT fk_quote_observation_display FOREIGN KEY (display_asset_id)
        REFERENCES public.assets(asset_id) ON DELETE RESTRICT,
    CONSTRAINT ck_quote_observation_pair CHECK (
        display_asset_id IN ('USD', 'GBP', 'EUR', 'HKD', 'CNY')
        AND asset_id <> display_asset_id
    ),
    CONSTRAINT ck_quote_observation_value CHECK (
        price ~ '^(0|[1-9][0-9]{0,19})([.][0-9]{1,18})?$' AND price ~ '[1-9]'
    ),
    CONSTRAINT ck_quote_observation_source_time CHECK (
        (source = 'Frankfurter / ECB' AND observed_at IS NULL)
        OR (source = 'Coinbase Exchange' AND observed_at IS NOT NULL)
    ),
    CONSTRAINT ck_quote_observation_dates CHECK (
        isfinite(price_date) AND isfinite(fetched_at)
        AND price_date <= (fetched_at AT TIME ZONE 'UTC')::date
        AND (observed_at IS NULL OR (
            isfinite(observed_at) AND observed_at <= fetched_at
            AND price_date = (observed_at AT TIME ZONE 'UTC')::date
        ))
    ),
    CONSTRAINT uq_quote_observation_original UNIQUE NULLS NOT DISTINCT (
        owner_id, asset_id, display_asset_id, source, price_date, observed_at, price
    )
);

CREATE INDEX ix_quote_observation_day ON public.quote_observations
    (owner_id, display_asset_id, asset_id, price_date, observed_at);

CREATE FUNCTION public.coinpup_seal_quote_observation() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog AS $$
BEGIN
    RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='Market observations are immutable',
        CONSTRAINT='ck_quote_observation_immutable';
END; $$;

CREATE FUNCTION public.coinpup_notify_quote_observation() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog AS $$
BEGIN
    PERFORM pg_advisory_xact_lock(18945999704708432);
    INSERT INTO public.change_log(owner_id, entity_type, entity_id, entity_version, change_kind)
        VALUES (NEW.owner_id, 'quote_observations', NEW.id::text, 1, 'upsert');
    RETURN NEW;
END; $$;

CREATE TRIGGER tr_quote_observation_seal BEFORE UPDATE OR DELETE
    ON public.quote_observations FOR EACH ROW EXECUTE FUNCTION public.coinpup_seal_quote_observation();
CREATE TRIGGER tr_quote_observation_change AFTER INSERT ON public.quote_observations
    FOR EACH ROW EXECUTE FUNCTION public.coinpup_notify_quote_observation();
