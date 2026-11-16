CREATE OR REPLACE FUNCTION public.coinpup_validate_shape_unallocation(journal_identifier uuid, common_result jsonb)
 RETURNS void
 LANGUAGE plpgsql
 SET search_path TO 'pg_catalog'
AS $function$
DECLARE return_identifier uuid;
BEGIN
    SELECT return_id INTO return_identifier FROM public.business_allocation_return_lines
        WHERE journal_id=journal_identifier;
    PERFORM public.coinpup_validate_business_allocation_return(return_identifier);
    IF common_result->>'error_message' IS NOT NULL THEN
        RAISE EXCEPTION USING ERRCODE=common_result->>'error_sqlstate',
            MESSAGE=common_result->>'error_message', CONSTRAINT=common_result->>'error_constraint';
    END IF;
END; $function$
