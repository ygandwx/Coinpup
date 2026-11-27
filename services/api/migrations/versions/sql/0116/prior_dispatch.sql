CREATE OR REPLACE FUNCTION coinpup_validate_posting_shape(journal_identifier uuid)
RETURNS void LANGUAGE plpgsql SET search_path=pg_catalog AS $$
DECLARE header public.journals%ROWTYPE; operation public.financial_operations%ROWTYPE;
        common_result jsonb;
BEGIN
    SELECT * INTO header FROM public.journals WHERE id=journal_identifier FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='Journal is missing',
            CONSTRAINT='ck_journal_shape';
    END IF;
    SELECT * INTO operation FROM public.financial_operations WHERE id=header.operation_id;
    IF NOT FOUND OR operation.ledger_id <> header.ledger_id OR
       header.journal_kind <> 'posting' OR header.reverses_journal_id IS NOT NULL THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='Posting journal identity is invalid',
            CONSTRAINT='ck_journal_shape';
    END IF;
    common_result := public.coinpup_validate_journal_common(journal_identifier);
    CASE operation.kind
        WHEN 'opening' THEN
            PERFORM public.coinpup_validate_shape_opening(journal_identifier, common_result);
        WHEN 'income' THEN
            PERFORM public.coinpup_validate_shape_income(journal_identifier, common_result);
        WHEN 'expense' THEN
            PERFORM public.coinpup_validate_shape_expense(journal_identifier, common_result);
        WHEN 'transfer' THEN
            PERFORM public.coinpup_validate_shape_transfer(journal_identifier, common_result);
        WHEN 'exchange' THEN
            PERFORM public.coinpup_validate_shape_exchange(journal_identifier, common_result);
        WHEN 'invoice_accrual' THEN
            PERFORM public.coinpup_validate_shape_invoice_accrual(
                journal_identifier, common_result);
        WHEN 'bill_accrual' THEN
            PERFORM public.coinpup_validate_shape_bill_accrual(
                journal_identifier, common_result);
        WHEN 'payment' THEN
            PERFORM public.coinpup_validate_shape_payment(journal_identifier, common_result);
        WHEN 'allocation' THEN
            PERFORM public.coinpup_validate_shape_allocation(journal_identifier, common_result);
        WHEN 'unallocation' THEN
            PERFORM public.coinpup_validate_shape_unallocation(
                journal_identifier, common_result);
        WHEN 'refund' THEN
            PERFORM public.coinpup_validate_shape_refund(journal_identifier, common_result);
        WHEN 'invoice_credit', 'bill_credit' THEN
            PERFORM public.coinpup_validate_shape_credit(journal_identifier, common_result);
        WHEN 'advance', 'repayment' THEN
            PERFORM public.coinpup_validate_interledger_journal(journal_identifier);
        ELSE RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='Unknown operation kind',
            CONSTRAINT='ck_journal_shape';
    END CASE;
END; $$;
