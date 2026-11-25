CREATE TABLE public.reminder_inbox_deliveries (
    id uuid PRIMARY KEY,
    ledger_id uuid NOT NULL,
    event_id uuid NOT NULL,
    event_revision_id uuid NOT NULL,
    config_revision_id uuid NOT NULL,
    channel varchar(8) NOT NULL DEFAULT 'inbox',
    version integer NOT NULL DEFAULT 1,
    planned_at timestamptz NOT NULL,
    delivered_at timestamptz NOT NULL,
    read_at timestamptz,
    title varchar(160) NOT NULL,
    event_kind varchar(12) NOT NULL,
    due_date date NOT NULL,
    date_basis varchar(10) NOT NULL,
    locale varchar(2) NOT NULL,
    CONSTRAINT uq_reminder_inbox_schedule UNIQUE (event_id, channel, planned_at),
    CONSTRAINT fk_reminder_inbox_event FOREIGN KEY (event_id, ledger_id)
        REFERENCES public.reminder_events(id, ledger_id) ON DELETE RESTRICT,
    CONSTRAINT fk_reminder_inbox_event_revision FOREIGN KEY (event_revision_id, ledger_id)
        REFERENCES public.reminder_event_revisions(id, ledger_id) ON DELETE RESTRICT,
    CONSTRAINT fk_reminder_inbox_config_revision FOREIGN KEY (config_revision_id, ledger_id)
        REFERENCES public.reminder_delivery_revisions(id, ledger_id) ON DELETE RESTRICT,
    CONSTRAINT ck_reminder_inbox_channel CHECK (channel = 'inbox'),
    CONSTRAINT ck_reminder_inbox_kind CHECK (event_kind IN ('annual','tax','certificate')),
    CONSTRAINT ck_reminder_inbox_locale CHECK (locale IN ('en','zh')),
    CONSTRAINT ck_reminder_inbox_date_basis CHECK (date_basis IN ('manual','calculated')),
    CONSTRAINT ck_reminder_inbox_delivered CHECK (planned_at <= delivered_at),
    CONSTRAINT ck_reminder_inbox_read_version CHECK (
        (version = 1 AND read_at IS NULL) OR
        (version = 2 AND read_at IS NOT NULL AND read_at >= delivered_at)
    )
);
CREATE INDEX ix_reminder_inbox_ledger_schedule ON public.reminder_inbox_deliveries(ledger_id, planned_at, id);

CREATE FUNCTION public.coinpup_guard_reminder_inbox_delivery() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog AS $$
DECLARE evidence record;
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION USING ERRCODE='23514', CONSTRAINT='ck_reminder_inbox_immutable',
            MESSAGE='Inbox history cannot be deleted';
    END IF;
    IF current_setting('transaction_isolation') <> 'read committed' THEN
        RAISE EXCEPTION USING ERRCODE='23514', CONSTRAINT='ck_reminder_inbox_isolation',
            MESSAGE='Inbox writes require read committed';
    END IF;
    PERFORM pg_advisory_xact_lock(18945999704708432);
    IF TG_OP = 'UPDATE' THEN
        IF OLD.version <> 1 OR NEW.version <> 2
           OR NEW.read_at IS DISTINCT FROM OLD.read_at
           OR (to_jsonb(NEW) - ARRAY['version','read_at'])
               IS DISTINCT FROM (to_jsonb(OLD) - ARRAY['version','read_at']) THEN
            RAISE EXCEPTION USING ERRCODE='23514', CONSTRAINT='ck_reminder_inbox_immutable',
                MESSAGE='Only the first read may be recorded';
        END IF;
        NEW.read_at := greatest(OLD.delivered_at, clock_timestamp());
        RETURN NEW;
    END IF;
    IF NEW.version <> 1 OR NEW.read_at IS NOT NULL THEN
        RAISE EXCEPTION USING ERRCODE='23514', CONSTRAINT='ck_reminder_inbox_read_version',
            MESSAGE='New inbox delivery must be unread';
    END IF;
    SELECT e.title, e.event_kind, e.manual_due_date, c.planned_for_date, c.locale
      INTO evidence
      FROM public.reminder_events e
      JOIN public.reminder_event_revisions er
        ON (er.ledger_id, er.event_id, er.version) = (e.ledger_id, e.id, e.version)
      JOIN public.reminder_delivery_configs c ON (c.ledger_id, c.event_id) = (e.ledger_id, e.id)
      JOIN public.reminder_delivery_revisions cr
        ON (cr.ledger_id, cr.config_id, cr.version) = (c.ledger_id, c.id, c.version)
      JOIN public.ledgers l ON l.id = e.ledger_id
      JOIN public.entities entity ON (entity.id, entity.owner_id) = (l.entity_id, l.owner_id)
      WHERE (e.id, e.ledger_id) = (NEW.event_id, NEW.ledger_id)
        AND er.id = NEW.event_revision_id AND cr.id = NEW.config_revision_id
        AND NOT e.completed AND NOT e.archived AND NOT entity.archived
        AND c.enabled AND c.inbox_enabled AND c.blocked_reason IS NULL
        AND c.planned_for_date = coalesce(e.manual_due_date,
            CASE WHEN e.evaluation_status = 'calculated' THEN e.calculated_date END)
        AND (c.lead_days, c.timezone, c.minute) = (c.planned_lead_days, c.planned_timezone, c.planned_minute)
        AND c.planned_at = NEW.planned_at AND NEW.planned_at <= clock_timestamp();
    IF NOT FOUND THEN
        RAISE EXCEPTION USING ERRCODE='23514', CONSTRAINT='ck_reminder_inbox_witness',
            MESSAGE='Current eligible evidence is required for a due inbox notice';
    END IF;
    NEW.title := evidence.title;
    NEW.event_kind := evidence.event_kind;
    NEW.locale := evidence.locale;
    NEW.due_date := evidence.planned_for_date;
    NEW.date_basis := CASE WHEN evidence.manual_due_date IS NULL THEN 'calculated' ELSE 'manual' END;
    NEW.delivered_at := clock_timestamp();
    RETURN NEW;
END; $$;

CREATE FUNCTION public.coinpup_notify_reminder_inbox_delivery() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog AS $$
BEGIN
    PERFORM pg_advisory_xact_lock(18945999704708432);
    INSERT INTO public.change_log(owner_id, ledger_id, entity_type, entity_id, entity_version, change_kind)
        SELECT l.owner_id, NEW.ledger_id, TG_TABLE_NAME, NEW.id::text, NEW.version, 'upsert'
        FROM public.ledgers l WHERE l.id = NEW.ledger_id;
    RETURN NEW;
END; $$;

CREATE TRIGGER tr_reminder_inbox_guard BEFORE INSERT OR UPDATE OR DELETE
    ON public.reminder_inbox_deliveries FOR EACH ROW
    EXECUTE FUNCTION public.coinpup_guard_reminder_inbox_delivery();
CREATE TRIGGER tr_reminder_inbox_change AFTER INSERT OR UPDATE
    ON public.reminder_inbox_deliveries FOR EACH ROW
    EXECUTE FUNCTION public.coinpup_notify_reminder_inbox_delivery();
