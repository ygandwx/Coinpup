CREATE TABLE public.reminder_email_deliveries (
    id uuid PRIMARY KEY,
    ledger_id uuid NOT NULL,
    event_id uuid NOT NULL,
    channel varchar(8) NOT NULL,
    planned_at timestamptz NOT NULL,
    CONSTRAINT uq_reminder_email_delivery_ledger UNIQUE (id, ledger_id),
    CONSTRAINT uq_reminder_email_schedule UNIQUE (event_id, channel, planned_at),
    CONSTRAINT fk_reminder_email_event FOREIGN KEY (event_id, ledger_id)
        REFERENCES public.reminder_events(id, ledger_id) ON DELETE RESTRICT,
    CONSTRAINT ck_reminder_email_channel CHECK (channel = 'email')
);

CREATE TABLE public.reminder_email_evidence (
    id uuid PRIMARY KEY,
    delivery_id uuid NOT NULL,
    ledger_id uuid NOT NULL,
    version integer NOT NULL,
    attempt_no integer NOT NULL,
    state varchar(24) NOT NULL,
    recorded_at timestamptz NOT NULL,
    retries_used integer NOT NULL,
    lease_until timestamptz,
    retry_at timestamptz,
    smtp_code integer,
    reason varchar(2000),
    event_revision_id uuid,
    config_revision_id uuid,
    envelope jsonb,
    CONSTRAINT uq_reminder_email_evidence_version UNIQUE (delivery_id, version),
    CONSTRAINT fk_reminder_email_evidence_delivery FOREIGN KEY (delivery_id, ledger_id)
        REFERENCES public.reminder_email_deliveries(id, ledger_id) ON DELETE RESTRICT,
    CONSTRAINT fk_reminder_email_evidence_event FOREIGN KEY (event_revision_id, ledger_id)
        REFERENCES public.reminder_event_revisions(id, ledger_id) ON DELETE RESTRICT,
    CONSTRAINT fk_reminder_email_evidence_config FOREIGN KEY (config_revision_id, ledger_id)
        REFERENCES public.reminder_delivery_revisions(id, ledger_id) ON DELETE RESTRICT,
    CONSTRAINT ck_reminder_email_evidence_sequence CHECK (
        version > 0 AND attempt_no > 0 AND retries_used BETWEEN 0 AND 5
    ),
    CONSTRAINT ck_reminder_email_evidence_state CHECK (
        state IN ('sending','retry_wait','failed','unknown','accepted','cancelled',
                  'verified_received','stopped')
    ),
    CONSTRAINT ck_reminder_email_evidence_claim CHECK (
        (state = 'sending' AND lease_until > recorded_at AND envelope IS NOT NULL
            AND jsonb_typeof(envelope) = 'object' AND event_revision_id IS NOT NULL
            AND config_revision_id IS NOT NULL AND lease_until IS NOT NULL) OR
        (state <> 'sending' AND lease_until IS NULL AND envelope IS NULL
            AND event_revision_id IS NULL AND config_revision_id IS NULL)
    ),
    CONSTRAINT ck_reminder_email_evidence_retry CHECK (
        (state = 'retry_wait' AND retry_at IS NOT NULL AND retry_at >= recorded_at) OR
        (state <> 'retry_wait' AND retry_at IS NULL)
    ),
    CONSTRAINT ck_reminder_email_evidence_smtp_code CHECK (
        smtp_code IS NULL OR (state = 'accepted' AND smtp_code BETWEEN 200 AND 299) OR
        (state = 'retry_wait' AND smtp_code BETWEEN 400 AND 499) OR
        (state = 'failed' AND smtp_code BETWEEN 400 AND 599)
    ),
    CONSTRAINT ck_reminder_email_evidence_reason CHECK (
        (state IN ('verified_received','stopped') AND reason IS NOT NULL AND btrim(reason) <> '') OR
        (state NOT IN ('verified_received','stopped') AND reason IS NULL)
    )
);

CREATE FUNCTION public.coinpup_guard_reminder_email_identity() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog AS $$
BEGIN
    IF TG_OP = 'INSERT' THEN
        PERFORM pg_advisory_xact_lock(18945999704708432);
        RETURN NEW;
    END IF;
    RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'Reminder email identity is immutable',
        CONSTRAINT = 'ck_reminder_email_identity_immutable';
END; $$;

CREATE FUNCTION public.coinpup_guard_reminder_email_evidence() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog AS $$
DECLARE delivery public.reminder_email_deliveries%ROWTYPE;
        prior public.reminder_email_evidence%ROWTYPE;
        current_event public.reminder_events%ROWTYPE;
        config public.reminder_delivery_configs%ROWTYPE;
        valid boolean := false;
        stale boolean;
        same_live_plan boolean;
BEGIN
    IF TG_OP <> 'INSERT' THEN
        RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'Reminder email evidence is immutable',
            CONSTRAINT = 'ck_reminder_email_evidence_immutable';
    END IF;
    PERFORM pg_advisory_xact_lock(18945999704708432);
    SELECT * INTO delivery FROM public.reminder_email_deliveries
        WHERE id = NEW.delivery_id AND ledger_id = NEW.ledger_id FOR UPDATE;
    SELECT * INTO prior FROM public.reminder_email_evidence
        WHERE delivery_id = delivery.id ORDER BY version DESC LIMIT 1;
    IF delivery.id IS NULL OR NEW.version <> coalesce(prior.version, 0) + 1
        OR NEW.recorded_at > clock_timestamp() THEN
        RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'Invalid reminder email evidence sequence',
            CONSTRAINT = 'ck_reminder_email_evidence_transition';
    END IF;
    IF NEW.state = 'sending' THEN
        SELECT * INTO current_event FROM public.reminder_events WHERE id = delivery.event_id;
        SELECT * INTO config FROM public.reminder_delivery_configs WHERE event_id = delivery.event_id;
        valid := (
            (prior.id IS NULL AND NEW.attempt_no = 1 AND NEW.retries_used = 0)
            OR (prior.state = 'retry_wait' AND prior.retry_at <= NEW.recorded_at
                AND NEW.attempt_no = prior.attempt_no + 1 AND NEW.retries_used = prior.retries_used)
        )
        AND NEW.lease_until = NEW.recorded_at + interval '90 seconds'
        AND NEW.lease_until > clock_timestamp()
        AND delivery.planned_at <= NEW.recorded_at
        AND current_event.ledger_id = delivery.ledger_id
        AND NOT current_event.archived AND NOT current_event.completed
        AND config.enabled AND config.email_enabled AND config.blocked_reason IS NULL
        AND config.planned_at = delivery.planned_at AND config.planned_for_date =
            coalesce(current_event.manual_due_date, current_event.calculated_date)
        AND EXISTS (SELECT 1 FROM public.ledgers ledger
            JOIN public.entities entity ON entity.id = ledger.entity_id
                AND entity.owner_id = ledger.owner_id
            WHERE ledger.id = delivery.ledger_id AND NOT entity.archived)
        AND EXISTS (SELECT 1 FROM public.reminder_event_revisions old_event
            WHERE old_event.id = NEW.event_revision_id AND old_event.event_id = current_event.id
                AND old_event.version = current_event.version AND old_event.ledger_id = delivery.ledger_id)
        AND EXISTS (SELECT 1 FROM public.reminder_delivery_revisions old_config
            WHERE old_config.id = NEW.config_revision_id AND old_config.config_id = config.id
                AND old_config.version = config.version AND old_config.ledger_id = delivery.ledger_id)
        AND jsonb_typeof(NEW.envelope -> 'sender') = 'string'
        AND jsonb_typeof(NEW.envelope -> 'recipient') = 'string'
        AND jsonb_typeof(NEW.envelope -> 'subject') = 'string'
        AND jsonb_typeof(NEW.envelope -> 'body') = 'string'
        AND NEW.envelope - ARRAY['sender','recipient','subject','body'] = '{}'::jsonb
        AND NEW.envelope ->> 'recipient' = config.email_recipient;
    ELSIF NEW.attempt_no = prior.attempt_no AND prior.state = 'sending' THEN
        IF NEW.state IN ('cancelled','retry_wait') AND NEW.retries_used = prior.retries_used THEN
            -- The worker stopped before sending. Only a changed lease on a still
            -- eligible slot can be reclaimed without consuming a transport retry.
            SELECT true,
                NOT entity.archived AND NOT event.archived AND NOT event.completed
                AND settings.enabled AND settings.email_enabled AND settings.blocked_reason IS NULL
                AND settings.planned_at = delivery.planned_at
                AND settings.planned_for_date = coalesce(
                    event.manual_due_date, event.calculated_date
                )
                INTO stale, same_live_plan
                FROM public.reminder_events event
                JOIN public.ledgers ledger ON ledger.id = event.ledger_id
                JOIN public.entities entity ON entity.id = ledger.entity_id
                JOIN public.reminder_delivery_configs settings ON settings.event_id = event.id
                WHERE event.id = delivery.event_id AND (
                    entity.archived OR NOT EXISTS (SELECT 1 FROM public.reminder_event_revisions rev
                        WHERE rev.id = prior.event_revision_id AND rev.version = event.version)
                    OR NOT EXISTS (SELECT 1 FROM public.reminder_delivery_revisions rev
                        WHERE rev.id = prior.config_revision_id AND rev.version = settings.version)
                );
        END IF;
        valid := (
            NEW.retries_used = prior.retries_used AND (
                NEW.state = 'unknown'
                OR (clock_timestamp() < prior.lease_until AND (
                    NEW.state = 'accepted'
                    OR (NEW.state = 'failed' AND (
                        NEW.smtp_code IS NULL OR NEW.smtp_code >= 500 OR prior.retries_used = 5
                    ))
                    OR (NEW.state = 'cancelled' AND stale)
                    OR (NEW.state = 'retry_wait' AND stale AND same_live_plan
                        AND NEW.smtp_code IS NULL AND NEW.retry_at = NEW.recorded_at)
                ))
            )
        ) OR (
            NEW.state = 'retry_wait' AND clock_timestamp() < prior.lease_until
            AND prior.retries_used < 5 AND NEW.retries_used = prior.retries_used + 1
            AND NEW.retry_at = NEW.recorded_at + (ARRAY[
                interval '1 minute', interval '5 minutes', interval '15 minutes',
                interval '60 minutes', interval '240 minutes'
            ])[NEW.retries_used]
        );
    ELSIF NEW.attempt_no = prior.attempt_no AND prior.state = 'retry_wait' THEN
        valid := NEW.state = 'cancelled' AND NEW.retries_used = prior.retries_used;
    ELSIF NEW.attempt_no = prior.attempt_no AND prior.state = 'failed' THEN
        valid := NEW.state = 'retry_wait' AND NEW.retries_used = 0
            AND NEW.smtp_code IS NULL AND NEW.retry_at = NEW.recorded_at;
    ELSIF NEW.attempt_no = prior.attempt_no AND prior.state = 'unknown' THEN
        valid := NEW.state IN ('verified_received','stopped') AND NEW.retries_used = prior.retries_used;
    END IF;
    IF valid IS DISTINCT FROM true THEN
        RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'Invalid reminder email evidence transition',
            CONSTRAINT = 'ck_reminder_email_evidence_transition';
    END IF;
    RETURN NEW;
END; $$;

CREATE FUNCTION public.coinpup_notify_reminder_email() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog AS $$
DECLARE owner uuid;
BEGIN
    PERFORM pg_advisory_xact_lock(18945999704708432);
    SELECT owner_id INTO owner FROM public.ledgers WHERE id = NEW.ledger_id;
    INSERT INTO public.change_log(owner_id, ledger_id, entity_type, entity_id, entity_version, change_kind)
        VALUES (owner, NEW.ledger_id, TG_TABLE_NAME, NEW.id::text, 1, 'upsert');
    IF TG_TABLE_NAME = 'reminder_email_evidence' THEN
        IF NEW.version > 1 THEN
            INSERT INTO public.change_log(owner_id, ledger_id, entity_type, entity_id, entity_version, change_kind)
                VALUES (owner, NEW.ledger_id, 'reminder_email_deliveries',
                    NEW.delivery_id::text, NEW.version, 'upsert');
        END IF;
    END IF;
    RETURN NULL;
END; $$;

CREATE FUNCTION public.coinpup_check_reminder_email_claim() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog AS $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM public.reminder_email_evidence WHERE delivery_id = NEW.id AND version = 1) THEN
        RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'Reminder email identity has no claim',
            CONSTRAINT = 'ck_reminder_email_identity_claim';
    END IF;
    RETURN NULL;
END; $$;

CREATE TRIGGER tr_reminder_email_delivery_guard BEFORE INSERT OR UPDATE OR DELETE
    ON public.reminder_email_deliveries FOR EACH ROW EXECUTE FUNCTION public.coinpup_guard_reminder_email_identity();
CREATE TRIGGER tr_reminder_email_evidence_guard BEFORE INSERT OR UPDATE OR DELETE
    ON public.reminder_email_evidence FOR EACH ROW EXECUTE FUNCTION public.coinpup_guard_reminder_email_evidence();
CREATE TRIGGER tr_reminder_email_delivery_change AFTER INSERT
    ON public.reminder_email_deliveries FOR EACH ROW EXECUTE FUNCTION public.coinpup_notify_reminder_email();
CREATE TRIGGER tr_reminder_email_evidence_change AFTER INSERT
    ON public.reminder_email_evidence FOR EACH ROW EXECUTE FUNCTION public.coinpup_notify_reminder_email();
CREATE CONSTRAINT TRIGGER zz_tr_reminder_email_delivery_claim AFTER INSERT
    ON public.reminder_email_deliveries DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION public.coinpup_check_reminder_email_claim();
