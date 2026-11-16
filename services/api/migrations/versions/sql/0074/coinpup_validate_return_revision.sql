CREATE OR REPLACE FUNCTION public.coinpup_validate_return_revision(revision_identifier uuid)
 RETURNS void
 LANGUAGE plpgsql
 SET search_path TO 'pg_catalog'
AS $function$
BEGIN
    RAISE EXCEPTION USING ERRCODE='23514',
        MESSAGE='Allocation return revision requires complete group validation',
        CONSTRAINT='ck_return_revision_shape';
END; $function$
