CREATE TABLE public.file_upload_resumes (
    upload_id uuid PRIMARY KEY,
    ledger_id uuid NOT NULL,
    owner_id uuid NOT NULL,
    device_id uuid NOT NULL,
    registry_version integer NOT NULL DEFAULT 1,
    sha256 varchar(64) NOT NULL,
    created_at timestamptz DEFAULT clock_timestamp() NOT NULL,
    abandoned_at timestamptz,
    CONSTRAINT uq_file_upload_resume_ledger UNIQUE (upload_id, ledger_id),
    CONSTRAINT fk_file_upload_resume_reservation FOREIGN KEY (upload_id, ledger_id)
        REFERENCES public.file_uploads(id, ledger_id) ON DELETE RESTRICT,
    CONSTRAINT fk_file_upload_resume_owner FOREIGN KEY (ledger_id, owner_id)
        REFERENCES public.ledgers(id, owner_id) ON DELETE RESTRICT,
    CONSTRAINT fk_file_upload_resume_device FOREIGN KEY (owner_id, device_id, registry_version)
        REFERENCES public.sync_device_revisions(owner_id, device_id, version) ON DELETE RESTRICT,
    CONSTRAINT ck_file_upload_resume_manifest CHECK (registry_version = 1 AND sha256 ~ '^[0-9a-f]{64}$')
);
CREATE INDEX ix_file_upload_resumes_owner_id ON public.file_upload_resumes(owner_id);

CREATE TABLE public.file_upload_chunks (
    upload_id uuid NOT NULL,
    byte_offset bigint NOT NULL,
    ledger_id uuid NOT NULL,
    byte_size integer NOT NULL,
    sha256 varchar(64) NOT NULL,
    payload bytea,
    PRIMARY KEY (upload_id, byte_offset),
    CONSTRAINT fk_file_upload_chunk_resume FOREIGN KEY (upload_id, ledger_id)
        REFERENCES public.file_upload_resumes(upload_id, ledger_id) ON DELETE RESTRICT,
    CONSTRAINT ck_file_upload_chunk_boundary CHECK (
        byte_offset >= 0 AND mod(byte_offset, 1048576) = 0 AND
        byte_size BETWEEN 1 AND 1048576 AND sha256 ~ '^[0-9a-f]{64}$'
    ),
    CONSTRAINT ck_file_upload_chunk_content CHECK (payload IS NULL OR (
        octet_length(payload) = byte_size AND encode(sha256(payload), 'hex') = sha256
    ))
);

CREATE FUNCTION public.coinpup_resume_device_active(owner uuid, device uuid) RETURNS boolean
LANGUAGE sql STABLE SET search_path = pg_catalog AS $$
    SELECT EXISTS (SELECT 1 FROM public.sync_device_revisions
        WHERE owner_id = owner AND device_id = device AND version = 1)
    AND NOT EXISTS (SELECT 1 FROM public.sync_device_revisions
        WHERE owner_id = owner AND device_id = device AND version = 2)
$$;

CREATE FUNCTION public.coinpup_guard_upload_resume() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog AS $$
DECLARE reservation public.file_uploads%ROWTYPE;
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION USING ERRCODE = '23514', CONSTRAINT = 'ck_upload_resume_immutable',
            MESSAGE = 'The resumable upload manifest is immutable';
    END IF;
    IF current_setting('transaction_isolation') <> 'read committed' THEN
        RAISE EXCEPTION USING ERRCODE = '23514', CONSTRAINT = 'ck_upload_resume_isolation',
            MESSAGE = 'Resumable uploads require read committed';
    END IF;
    PERFORM pg_advisory_xact_lock(18945999704708432);
    SELECT * INTO reservation FROM public.file_uploads
        WHERE id = NEW.upload_id AND ledger_id = NEW.ledger_id FOR UPDATE;
    IF TG_OP = 'UPDATE' THEN
        IF reservation.state = 'pending'
            AND OLD.abandoned_at IS NULL AND NEW.abandoned_at IS NOT NULL
            AND NEW.abandoned_at BETWEEN OLD.created_at AND clock_timestamp()
            AND ROW(NEW.upload_id, NEW.ledger_id, NEW.owner_id, NEW.device_id,
                    NEW.registry_version, NEW.sha256, NEW.created_at) IS NOT DISTINCT FROM
                ROW(OLD.upload_id, OLD.ledger_id, OLD.owner_id, OLD.device_id,
                    OLD.registry_version, OLD.sha256, OLD.created_at) THEN
            RETURN NEW;
        END IF;
        RAISE EXCEPTION USING ERRCODE = '23514', CONSTRAINT = 'ck_upload_resume_immutable',
            MESSAGE = 'The resumable upload manifest is immutable';
    END IF;
    IF reservation.created_by IS DISTINCT FROM NEW.owner_id
        OR NOT public.coinpup_resume_device_active(NEW.owner_id, NEW.device_id)
        OR reservation.state IS DISTINCT FROM 'pending' OR NEW.abandoned_at IS NOT NULL THEN
        RAISE EXCEPTION USING ERRCODE = '23514', CONSTRAINT = 'ck_upload_resume_scope',
            MESSAGE = 'The resumable upload manifest does not match the reservation';
    END IF;
    RETURN NEW;
END; $$;

CREATE FUNCTION public.coinpup_guard_upload_chunk() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog AS $$
DECLARE resume public.file_upload_resumes%ROWTYPE;
        reservation public.file_uploads%ROWTYPE;
        cursor_offset bigint;
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION USING ERRCODE = '23514', CONSTRAINT = 'ck_upload_chunk_immutable',
            MESSAGE = 'Upload part receipts are immutable';
    END IF;
    IF current_setting('transaction_isolation') <> 'read committed' THEN
        RAISE EXCEPTION USING ERRCODE = '23514', CONSTRAINT = 'ck_upload_resume_isolation',
            MESSAGE = 'Resumable uploads require read committed';
    END IF;
    PERFORM pg_advisory_xact_lock(18945999704708432);
    SELECT * INTO reservation FROM public.file_uploads
        WHERE id = NEW.upload_id AND ledger_id = NEW.ledger_id FOR UPDATE;
    SELECT * INTO resume FROM public.file_upload_resumes
        WHERE upload_id = NEW.upload_id AND ledger_id = NEW.ledger_id;
    IF TG_OP = 'UPDATE' THEN
        IF (reservation.state = 'ready' OR resume.abandoned_at IS NOT NULL)
            AND NEW.payload IS NULL AND OLD.payload IS NOT NULL
            AND ROW(NEW.upload_id, NEW.ledger_id, NEW.byte_offset, NEW.byte_size, NEW.sha256)
                IS NOT DISTINCT FROM
                ROW(OLD.upload_id, OLD.ledger_id, OLD.byte_offset, OLD.byte_size, OLD.sha256) THEN
            RETURN NEW;
        END IF;
        RAISE EXCEPTION USING ERRCODE = '23514', CONSTRAINT = 'ck_upload_chunk_immutable',
            MESSAGE = 'Upload part receipts are immutable';
    END IF;
    SELECT coalesce(max(byte_offset + byte_size), 0) INTO cursor_offset
        FROM public.file_upload_chunks WHERE upload_id = NEW.upload_id;
    IF reservation.state IS DISTINCT FROM 'pending' OR resume.upload_id IS NULL
        OR resume.abandoned_at IS NOT NULL
        OR NOT public.coinpup_resume_device_active(resume.owner_id, resume.device_id)
        OR NOT EXISTS (SELECT 1 FROM public.ledgers ledger
            JOIN public.entities entity ON entity.id = ledger.entity_id AND entity.owner_id = ledger.owner_id
            WHERE ledger.id = resume.ledger_id AND NOT entity.archived)
        OR NEW.payload IS NULL OR NEW.byte_offset <> cursor_offset
        OR NEW.byte_size <> least(1048576, reservation.declared_size - cursor_offset) THEN
        RAISE EXCEPTION USING ERRCODE = '23514', CONSTRAINT = 'ck_upload_chunk_append',
            MESSAGE = 'The upload part does not match the active reservation and committed offset';
    END IF;
    RETURN NEW;
END; $$;

CREATE FUNCTION public.coinpup_complete_resumable_upload() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog AS $$
DECLARE resume public.file_upload_resumes%ROWTYPE;
BEGIN
    IF OLD.state <> 'pending' OR NEW.state <> 'ready' THEN RETURN NULL; END IF;
    PERFORM pg_advisory_xact_lock(18945999704708432);
    SELECT * INTO resume FROM public.file_upload_resumes
        WHERE upload_id = NEW.id AND ledger_id = NEW.ledger_id;
    IF resume.upload_id IS NULL THEN RETURN NULL; END IF;
    IF current_setting('transaction_isolation') <> 'read committed'
        OR resume.abandoned_at IS NOT NULL
        OR NOT public.coinpup_resume_device_active(resume.owner_id, resume.device_id)
        OR NOT EXISTS (SELECT 1 FROM public.stored_files file
            WHERE file.id = NEW.file_id AND file.ledger_id = NEW.ledger_id
                AND file.sha256 = resume.sha256 AND file.byte_size = NEW.declared_size) THEN
        RAISE EXCEPTION USING ERRCODE = '23514', CONSTRAINT = 'ck_upload_resume_complete',
            MESSAGE = 'The sealed upload does not match its resumable manifest';
    END IF;
    UPDATE public.file_upload_chunks SET payload = NULL
        WHERE upload_id = NEW.id AND payload IS NOT NULL;
    RETURN NULL;
END; $$;

CREATE FUNCTION public.coinpup_abandon_resumable_upload() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog AS $$
BEGIN
    UPDATE public.file_upload_chunks SET payload = NULL
        WHERE upload_id = NEW.upload_id AND ledger_id = NEW.ledger_id AND payload IS NOT NULL;
    RETURN NULL;
END; $$;

CREATE TRIGGER tr_file_upload_resume_guard BEFORE INSERT OR UPDATE OR DELETE
    ON public.file_upload_resumes FOR EACH ROW EXECUTE FUNCTION public.coinpup_guard_upload_resume();
CREATE TRIGGER tr_file_upload_resume_abandon AFTER UPDATE
    ON public.file_upload_resumes FOR EACH ROW
    WHEN (OLD.abandoned_at IS NULL AND NEW.abandoned_at IS NOT NULL)
    EXECUTE FUNCTION public.coinpup_abandon_resumable_upload();
CREATE TRIGGER tr_file_upload_chunk_guard BEFORE INSERT OR UPDATE OR DELETE
    ON public.file_upload_chunks FOR EACH ROW EXECUTE FUNCTION public.coinpup_guard_upload_chunk();
CREATE TRIGGER tr_resumable_upload_complete AFTER UPDATE ON public.file_uploads
    FOR EACH ROW EXECUTE FUNCTION public.coinpup_complete_resumable_upload();
