CREATE TABLE public.sync_financial_pushes (
    owner_id uuid NOT NULL,
    device_id uuid NOT NULL,
    intent_id uuid NOT NULL,
    registration_version integer NOT NULL DEFAULT 1,
    ledger_id uuid NOT NULL,
    operation_id uuid NOT NULL,
    command_key varchar(128) NOT NULL,
    request_hash varchar(64) NOT NULL,
    ledger_digest varchar(64) NOT NULL,
    request_body text NOT NULL,
    outcome varchar(8) NOT NULL,
    receipt_key varchar(128),
    basis_version integer,
    competing_version integer,
    response jsonb NOT NULL,
    recorded_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (owner_id, device_id, intent_id),
    CONSTRAINT fk_sync_financial_push_device
        FOREIGN KEY (owner_id, device_id, registration_version)
        REFERENCES public.sync_device_revisions(owner_id, device_id, version) ON DELETE RESTRICT,
    CONSTRAINT fk_sync_financial_push_ledger FOREIGN KEY (ledger_id, owner_id)
        REFERENCES public.ledgers(id, owner_id) ON DELETE RESTRICT,
    CONSTRAINT fk_sync_financial_push_operation FOREIGN KEY (operation_id, ledger_id)
        REFERENCES public.financial_operations(id, ledger_id) ON DELETE RESTRICT,
    CONSTRAINT fk_sync_financial_push_receipt FOREIGN KEY (ledger_id, receipt_key)
        REFERENCES public.command_receipts(ledger_id, key) ON DELETE RESTRICT,
    CONSTRAINT ck_sync_financial_push_registration CHECK (registration_version = 1),
    CONSTRAINT ck_sync_financial_push_key CHECK (command_key ~ '^[!-~]{1,128}$'),
    CONSTRAINT ck_sync_financial_push_hashes CHECK (
        request_hash ~ '^[0-9a-f]{64}$' AND ledger_digest ~ '^[0-9a-f]{64}$'
    ),
    CONSTRAINT ck_sync_financial_push_body CHECK (octet_length(request_body) BETWEEN 1 AND 131072),
    CONSTRAINT ck_sync_financial_push_response CHECK (
        jsonb_typeof(response) = 'object' AND response->>'outcome' IS NOT DISTINCT FROM outcome
    ),
    CONSTRAINT ck_sync_financial_push_outcome CHECK (
        (outcome = 'applied' AND receipt_key IS NOT NULL AND receipt_key = command_key
         AND basis_version IS NULL AND competing_version IS NULL)
        OR (outcome = 'conflict' AND receipt_key IS NULL AND basis_version IS NOT NULL
            AND competing_version IS NOT NULL AND basis_version > 0
            AND competing_version > basis_version)
    )
);

CREATE TABLE public.sync_financial_conflict_resolutions (
    owner_id uuid NOT NULL,
    device_id uuid NOT NULL,
    intent_id uuid NOT NULL,
    accepted_version integer NOT NULL,
    resolved_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (owner_id, device_id, intent_id),
    CONSTRAINT fk_sync_financial_resolution_push FOREIGN KEY (owner_id, device_id, intent_id)
        REFERENCES public.sync_financial_pushes(owner_id, device_id, intent_id) ON DELETE RESTRICT,
    CONSTRAINT ck_sync_financial_resolution_version CHECK (accepted_version > 0)
);

CREATE FUNCTION public.coinpup_guard_sync_financial_push() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog AS $$
BEGIN
    IF TG_OP <> 'INSERT' THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='Device push outcomes are immutable',
            CONSTRAINT='ck_sync_financial_push_immutable';
    END IF;
    IF current_setting('transaction_isolation') <> 'read committed' THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='Device pushes require read committed',
            CONSTRAINT='ck_sync_protocol_isolation';
    END IF;
    PERFORM pg_advisory_xact_lock(18945999704708432);
    IF EXISTS (
        SELECT 1 FROM public.sync_device_revisions d
        WHERE (d.owner_id, d.device_id) = (NEW.owner_id, NEW.device_id) AND d.version = 2
    ) THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='Device is retired',
            CONSTRAINT='ck_sync_financial_push_retired';
    END IF;
    IF (NEW.response->>'device_id', NEW.response->>'intent_id',
        NEW.response->>'ledger_id', NEW.response->>'operation_id') IS DISTINCT FROM
       (NEW.device_id::text, NEW.intent_id::text, NEW.ledger_id::text, NEW.operation_id::text) THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='Device push response identity does not match',
            CONSTRAINT='ck_sync_financial_push_response_identity';
    END IF;
    IF NEW.outcome = 'applied' AND NOT EXISTS (
        SELECT 1 FROM public.command_receipts r
        WHERE (r.ledger_id, r.key, r.operation_id) = (NEW.ledger_id, NEW.receipt_key, NEW.operation_id)
            AND (r.hash_version = 1 OR r.request_hash = NEW.ledger_digest)
            AND r.response = NEW.response->'receipt'
    ) THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='Device push receipt does not match',
            CONSTRAINT='ck_sync_financial_push_receipt_fact';
    END IF;
    RETURN NEW;
END; $$;

CREATE FUNCTION public.coinpup_guard_sync_financial_resolution() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog AS $$
BEGIN
    IF TG_OP <> 'INSERT' THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='Device resolutions are immutable',
            CONSTRAINT='ck_sync_financial_resolution_immutable';
    END IF;
    IF current_setting('transaction_isolation') <> 'read committed' THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='Device resolutions require read committed',
            CONSTRAINT='ck_sync_protocol_isolation';
    END IF;
    PERFORM pg_advisory_xact_lock(18945999704708432);
    IF EXISTS (
        SELECT 1 FROM public.sync_device_revisions d
        WHERE (d.owner_id, d.device_id) = (NEW.owner_id, NEW.device_id) AND d.version = 2
    ) THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='Device is retired',
            CONSTRAINT='ck_sync_financial_resolution_retired';
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM public.sync_financial_pushes p
        JOIN public.financial_operations o ON (o.id, o.ledger_id) = (p.operation_id, p.ledger_id)
        WHERE (p.owner_id, p.device_id, p.intent_id) = (NEW.owner_id, NEW.device_id, NEW.intent_id)
            AND p.outcome = 'conflict' AND o.version = NEW.accepted_version
    ) THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='Device resolution does not match',
            CONSTRAINT='ck_sync_financial_resolution_fact';
    END IF;
    RETURN NEW;
END; $$;

CREATE TRIGGER tr_sync_financial_push_guard BEFORE INSERT OR UPDATE OR DELETE
    ON public.sync_financial_pushes FOR EACH ROW EXECUTE FUNCTION public.coinpup_guard_sync_financial_push();
CREATE TRIGGER tr_sync_financial_resolution_guard BEFORE INSERT OR UPDATE OR DELETE
    ON public.sync_financial_conflict_resolutions FOR EACH ROW
    EXECUTE FUNCTION public.coinpup_guard_sync_financial_resolution();
