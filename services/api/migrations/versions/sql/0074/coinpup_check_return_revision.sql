CREATE OR REPLACE FUNCTION public.coinpup_check_return_revision()
 RETURNS trigger
 LANGUAGE plpgsql
 SET search_path TO 'pg_catalog'
AS $function$
DECLARE earliest_refund date;
BEGIN
    SELECT min(e.refund_date) INTO earliest_refund
    FROM public.coinpup_current_refund_return_references(
        NEW.ledger_id, NEW.payment_id, ARRAY[NEW.return_id]
    ) e;
    IF earliest_refund IS NOT NULL THEN
        IF NEW.action='cancel' THEN
            RAISE EXCEPTION USING ERRCODE='23514',
                MESSAGE='Correct each effective refund reference before cancelling the return',
                CONSTRAINT='ck_return_revision_refund_reference';
        END IF;
        IF NEW.return_date > earliest_refund THEN
            RAISE EXCEPTION USING ERRCODE='23514',
                MESSAGE='A referenced return cannot follow the effective refund date',
                CONSTRAINT='ck_return_revision_refund_date';
        END IF;
    END IF;
    PERFORM public.coinpup_validate_return_revision(NEW.id);
    RETURN NULL;
END; $function$
