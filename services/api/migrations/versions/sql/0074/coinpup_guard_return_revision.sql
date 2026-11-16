CREATE OR REPLACE FUNCTION public.coinpup_guard_return_revision()
 RETURNS trigger
 LANGUAGE plpgsql
 SET search_path TO 'pg_catalog'
AS $function$
BEGIN
    IF TG_OP <> 'INSERT' THEN
        RAISE EXCEPTION USING ERRCODE='23514',
            MESSAGE='Allocation return revision history is immutable',
            CONSTRAINT='ck_return_revision_immutable';
    END IF;
    PERFORM pg_advisory_xact_lock(18945999704708432);
    IF current_setting('transaction_isolation') <> 'read committed' THEN
        RAISE EXCEPTION USING ERRCODE='23514',
            MESSAGE='Allocation return revision isolation is invalid',
            CONSTRAINT='ck_return_revision_isolation';
    END IF;
    RETURN NEW;
END; $function$
