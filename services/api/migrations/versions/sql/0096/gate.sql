CREATE OR REPLACE FUNCTION public.coinpup_check_document_revision()
 RETURNS trigger
 LANGUAGE plpgsql
 SET search_path TO 'pg_catalog'
AS $function$
BEGIN
    RAISE EXCEPTION USING ERRCODE='23514',
        MESSAGE='A document revision requires complete confirmed facts and accruals',
        CONSTRAINT='ck_document_revision_complete';
END; $function$;
