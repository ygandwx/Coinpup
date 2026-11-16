CREATE OR REPLACE FUNCTION public.coinpup_check_allocation_return()
 RETURNS trigger
 LANGUAGE plpgsql
 SET search_path TO 'pg_catalog'
AS $function$
BEGIN
    IF TG_TABLE_NAME='business_allocation_returns' THEN
        PERFORM public.coinpup_validate_business_allocation_return(NEW.id);
    ELSE
        PERFORM public.coinpup_validate_business_allocation_return(NEW.return_id);
    END IF;
    RETURN NULL;
END; $function$
