CREATE OR REPLACE FUNCTION public.coinpup_check_return_revision_member()
 RETURNS trigger
 LANGUAGE plpgsql
 SET search_path TO 'pg_catalog'
AS $function$
BEGIN
    PERFORM public.coinpup_validate_return_revision(NEW.revision_id);
    RETURN NULL;
END; $function$
