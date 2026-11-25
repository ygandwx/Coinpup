CREATE TABLE public.sync_device_revisions (
    owner_id uuid NOT NULL REFERENCES public.administrators(id) ON DELETE RESTRICT,
    device_id uuid NOT NULL,
    version integer NOT NULL,
    previous_version integer,
    start_seq bigint NOT NULL DEFAULT 0,
    state varchar(10) NOT NULL,
    recorded_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY(owner_id,device_id,version),
    CONSTRAINT ck_sync_device_start_seq CHECK (start_seq >= 0),
    CONSTRAINT ck_sync_device_transition CHECK (
        (version=1 AND state='registered' AND previous_version IS NULL)
        OR (version=2 AND state='retired' AND previous_version IS NOT NULL
            AND previous_version=1)),
    CONSTRAINT ck_sync_device_time CHECK (
        isfinite(recorded_at)
        AND EXTRACT(YEAR FROM recorded_at AT TIME ZONE 'UTC') BETWEEN 1 AND 9999),
    CONSTRAINT fk_sync_device_origin FOREIGN KEY(owner_id,device_id,previous_version)
        REFERENCES public.sync_device_revisions(owner_id,device_id,version) ON DELETE RESTRICT
);

CREATE FUNCTION public.coinpup_seal_device_revision() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog AS $$
BEGIN
    IF TG_OP <> 'INSERT' THEN
        RAISE EXCEPTION USING ERRCODE = '23514', CONSTRAINT = 'ck_sync_device_sealed',
            MESSAGE = 'Device history is immutable';
    END IF;
    IF current_setting('transaction_isolation') <> 'read committed' THEN
        RAISE EXCEPTION USING ERRCODE = '23514', CONSTRAINT = 'ck_sync_protocol_isolation',
            MESSAGE = 'Device changes require read committed';
    END IF;
    PERFORM pg_advisory_xact_lock(18945999704708432);
    IF (NEW.start_seq > 0 AND NOT EXISTS (SELECT 1 FROM public.change_log
            WHERE owner_id = NEW.owner_id AND seq = NEW.start_seq))
        OR (NEW.version = 2 AND NOT EXISTS (SELECT 1 FROM public.sync_device_revisions
            WHERE owner_id = NEW.owner_id AND device_id = NEW.device_id AND version = 1
            AND start_seq = NEW.start_seq)) THEN
        RAISE EXCEPTION USING ERRCODE = '23514', CONSTRAINT = 'ck_sync_device_start',
            MESSAGE = 'The registered start differs';
    END IF;
    RETURN NEW;
END; $$;

CREATE TRIGGER trg_sync_device_sealed BEFORE INSERT OR UPDATE OR DELETE ON public.sync_device_revisions
FOR EACH ROW EXECUTE FUNCTION public.coinpup_seal_device_revision();

CREATE TABLE public.sync_page_acknowledgements (
    owner_id uuid NOT NULL,
    device_id uuid NOT NULL,
    registry_version integer NOT NULL DEFAULT 1,
    after_seq bigint NOT NULL,
    through_seq bigint NOT NULL,
    page_limit integer NOT NULL,
    row_count integer NOT NULL,
    last_seq bigint NOT NULL,
    prior_tail bigint,
    recorded_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (owner_id, device_id, last_seq),
    CONSTRAINT uq_sync_ack_start UNIQUE (owner_id,device_id,after_seq),
    CONSTRAINT fk_sync_ack_registration FOREIGN KEY (owner_id,device_id,registry_version)
        REFERENCES public.sync_device_revisions(owner_id,device_id,version) ON DELETE RESTRICT,
    CONSTRAINT fk_sync_ack_prior FOREIGN KEY (owner_id,device_id,prior_tail)
        REFERENCES public.sync_page_acknowledgements(owner_id,device_id,last_seq)
        ON DELETE RESTRICT,
    CONSTRAINT ck_sync_ack_window CHECK (
        registry_version=1 AND after_seq>=0 AND last_seq>after_seq AND through_seq>=last_seq
        AND page_limit BETWEEN 1 AND 200 AND row_count BETWEEN 1 AND page_limit
        AND (prior_tail IS NULL OR after_seq=prior_tail)),
    CONSTRAINT ck_sync_ack_time CHECK (
        isfinite(recorded_at)
        AND EXTRACT(YEAR FROM recorded_at AT TIME ZONE 'UTC') BETWEEN 1 AND 9999)
);

CREATE UNIQUE INDEX uq_sync_ack_single_start
ON public.sync_page_acknowledgements(owner_id,device_id) WHERE prior_tail IS NULL;

CREATE FUNCTION public.coinpup_check_sync_ack() RETURNS trigger
LANGUAGE plpgsql SET search_path=pg_catalog AS $$
DECLARE n integer; tail bigint; BEGIN
    IF TG_OP <> 'INSERT' THEN
        RAISE EXCEPTION USING ERRCODE='23514', CONSTRAINT='ck_sync_ack_sealed',
            MESSAGE='Sync acknowledgements are immutable';
    END IF;
    IF current_setting('transaction_isolation') <> 'read committed' THEN
        RAISE EXCEPTION USING ERRCODE = '23514', CONSTRAINT = 'ck_sync_protocol_isolation',
            MESSAGE = 'Page acknowledgements require read committed';
    END IF;
    PERFORM pg_advisory_xact_lock(18945999704708432);
    IF EXISTS (SELECT 1 FROM public.sync_device_revisions d WHERE d.owner_id=NEW.owner_id
               AND d.device_id=NEW.device_id AND d.version=2)
       OR (NEW.prior_tail IS NULL AND NOT EXISTS (SELECT 1 FROM public.sync_device_revisions d
               WHERE d.owner_id = NEW.owner_id AND d.device_id = NEW.device_id AND d.version = 1
               AND d.start_seq = NEW.after_seq))
       OR (NEW.after_seq>0 AND NOT EXISTS (SELECT 1 FROM public.change_log c
               WHERE c.owner_id=NEW.owner_id AND c.seq=NEW.after_seq))
       OR NOT EXISTS (SELECT 1 FROM public.change_log c
               WHERE c.owner_id=NEW.owner_id AND c.seq=NEW.through_seq)
       OR NEW.page_limit NOT BETWEEN 1 AND 200 THEN
        RAISE EXCEPTION USING ERRCODE='23514', CONSTRAINT='ck_sync_ack_scope',
            MESSAGE='Device or acknowledged window is unavailable';
    END IF;
    SELECT count(*), max(p.seq) INTO n, tail FROM (
        SELECT c.seq FROM public.change_log c WHERE c.owner_id=NEW.owner_id
          AND c.seq>NEW.after_seq AND c.seq<=NEW.through_seq
        ORDER BY c.seq LIMIT NEW.page_limit
    ) p;
    IF n=0 OR tail IS DISTINCT FROM NEW.last_seq OR n<>NEW.row_count THEN
        RAISE EXCEPTION USING ERRCODE='23514', CONSTRAINT='ck_sync_ack_prefix',
            MESSAGE='Acknowledgement differs from the committed page';
    END IF;
    RETURN NEW;
END $$;

CREATE TRIGGER trg_sync_ack_guard BEFORE INSERT OR UPDATE OR DELETE
ON public.sync_page_acknowledgements FOR EACH ROW EXECUTE FUNCTION public.coinpup_check_sync_ack();
