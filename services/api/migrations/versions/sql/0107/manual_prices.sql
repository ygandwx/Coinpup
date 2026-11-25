CREATE TABLE public.manual_prices (
    id uuid PRIMARY KEY,
    owner_id uuid NOT NULL,
    asset_id varchar(200) NOT NULL,
    display_asset_id varchar(200) NOT NULL,
    price_date date NOT NULL,
    price varchar(39) NOT NULL,
    source varchar(128) NOT NULL,
    version integer NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    CONSTRAINT fk_manual_price_owner FOREIGN KEY (owner_id)
        REFERENCES public.administrators(id) ON DELETE RESTRICT,
    CONSTRAINT fk_manual_price_base_asset FOREIGN KEY (asset_id)
        REFERENCES public.assets(asset_id) ON DELETE RESTRICT,
    CONSTRAINT fk_manual_price_display_asset FOREIGN KEY (display_asset_id)
        REFERENCES public.assets(asset_id) ON DELETE RESTRICT,
    CONSTRAINT uq_manual_price_pair_day UNIQUE (owner_id, price_date, display_asset_id, asset_id),
    CONSTRAINT ck_manual_price_pair CHECK (
        display_asset_id IN ('USD', 'GBP', 'EUR', 'HKD', 'CNY') AND asset_id <> display_asset_id
    ),
    CONSTRAINT ck_manual_price_value CHECK (
        price ~ '^(0|[1-9][0-9]{0,19})([.][0-9]{1,18})?$' AND price ~ '[1-9]'
    ),
    CONSTRAINT ck_manual_price_source CHECK (
        btrim(source) = source AND source <> '' AND source !~ '[[:cntrl:]]'
    ),
    CONSTRAINT ck_manual_price_version CHECK (version > 0)
);

CREATE TABLE public.manual_price_audits (
    price_id uuid NOT NULL,
    version integer NOT NULL,
    price varchar(39) NOT NULL,
    source varchar(128) NOT NULL,
    updated_at timestamptz NOT NULL,
    PRIMARY KEY (price_id, version),
    CONSTRAINT fk_manual_price_audit_head FOREIGN KEY (price_id)
        REFERENCES public.manual_prices(id) ON DELETE RESTRICT
);

CREATE FUNCTION public.coinpup_guard_manual_price_audit() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog AS $$
BEGIN
    IF TG_OP <> 'INSERT' OR NOT EXISTS (
        SELECT 1 FROM public.manual_prices
        WHERE (id, version, price, source, updated_at) =
              (NEW.price_id, NEW.version, NEW.price, NEW.source, NEW.updated_at)
    ) THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='Invalid manual price audit',
            CONSTRAINT='ck_manual_price_audit';
    END IF;
    RETURN NEW;
END; $$;

CREATE FUNCTION public.coinpup_guard_manual_price() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='Manual prices cannot be deleted',
            CONSTRAINT='ck_manual_price_identity';
    END IF;
    IF TG_OP = 'INSERT' THEN
        IF NEW.version <> 1 THEN
            RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='Manual price must start at version 1',
                CONSTRAINT='ck_manual_price_identity';
        END IF;
    ELSIF (NEW.id, NEW.owner_id, NEW.asset_id, NEW.display_asset_id, NEW.price_date, NEW.created_at)
          IS DISTINCT FROM
          (OLD.id, OLD.owner_id, OLD.asset_id, OLD.display_asset_id, OLD.price_date, OLD.created_at)
        OR NEW.version <> OLD.version + 1
        OR (NEW.price, NEW.source) IS NOT DISTINCT FROM (OLD.price, OLD.source) THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='Invalid manual price update',
            CONSTRAINT='ck_manual_price_identity';
    END IF;
    RETURN NEW;
END; $$;

CREATE FUNCTION public.coinpup_notify_manual_price() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog AS $$
BEGIN
    PERFORM pg_advisory_xact_lock(18945999704708432);
    INSERT INTO public.manual_price_audits(price_id, version, price, source, updated_at)
        VALUES (NEW.id, NEW.version, NEW.price, NEW.source, NEW.updated_at);
    INSERT INTO public.change_log(owner_id, entity_type, entity_id, entity_version, change_kind)
        VALUES (NEW.owner_id, 'manual_prices', NEW.id::text, NEW.version, 'upsert');
    RETURN NEW;
END; $$;

CREATE TRIGGER tr_manual_price_guard BEFORE INSERT OR UPDATE OR DELETE ON public.manual_prices
    FOR EACH ROW EXECUTE FUNCTION public.coinpup_guard_manual_price();
CREATE TRIGGER tr_manual_price_change AFTER INSERT OR UPDATE ON public.manual_prices
    FOR EACH ROW EXECUTE FUNCTION public.coinpup_notify_manual_price();
CREATE TRIGGER tr_manual_price_audit_guard BEFORE INSERT OR UPDATE OR DELETE
    ON public.manual_price_audits FOR EACH ROW EXECUTE FUNCTION
    public.coinpup_guard_manual_price_audit();
