CREATE FUNCTION public.coinpup_guard_platform_transfer() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog AS $$
DECLARE source_scale integer; destination_scale integer;
        source_mode text; destination_mode text;
        source_family text; destination_family text;
BEGIN
    IF current_setting('transaction_isolation') <> 'read committed' THEN
        RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'Platform transfers require read committed',
            CONSTRAINT = 'ck_platform_transfer_write_isolation';
    END IF;
    PERFORM pg_advisory_xact_lock(18945999704708432);
    IF TG_OP = 'UPDATE' THEN
        IF OLD.version = 1 AND NEW.version = 2
           AND (to_jsonb(NEW) - ARRAY['version', 'cancel_key']) IS NOT DISTINCT FROM
               (to_jsonb(OLD) - ARRAY['version', 'cancel_key']) THEN
            RETURN NEW;
        END IF;
    ELSIF TG_OP = 'INSERT' THEN
        SELECT account.custody_mode, asset.scale INTO source_mode, source_scale
        FROM public.account_assets link
        JOIN public.accounts account ON account.id = link.account_id
        JOIN public.assets asset ON asset.asset_id = link.asset_id
        WHERE (link.ledger_id, link.account_id, link.asset_id) =
              (NEW.source_ledger_id, NEW.source_account_id, NEW.source_asset_id)
          AND link.enabled AND asset.enabled AND NOT account.archived
          AND account.account_class = 'money';
        SELECT account.custody_mode, asset.scale INTO destination_mode, destination_scale
        FROM public.account_assets link
        JOIN public.accounts account ON account.id = link.account_id
        JOIN public.assets asset ON asset.asset_id = link.asset_id
        WHERE (link.ledger_id, link.account_id, link.asset_id) =
              (NEW.destination_ledger_id, NEW.destination_account_id, NEW.destination_asset_id)
          AND link.enabled AND asset.enabled AND NOT account.archived
          AND account.account_class = 'money';
        source_family := public.coinpup_custody_family(NEW.source_asset_id);
        destination_family := public.coinpup_custody_family(NEW.destination_asset_id);
        IF NEW.version = 1 AND source_family IS NOT NULL
           AND source_family = destination_family
           AND NEW.amount = trunc(NEW.amount, source_scale)
           AND NEW.amount = trunc(NEW.amount, destination_scale)
           AND (
                (NEW.kind = 'deposit' AND source_mode = 'onchain'
                    AND destination_mode = 'custodial') OR
                (NEW.kind = 'withdrawal' AND source_mode = 'custodial'
                    AND destination_mode = 'onchain')
           )
        THEN
            RETURN NEW;
        END IF;
    END IF;
    RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'Invalid platform transfer transition',
        CONSTRAINT = 'ck_platform_transfer_history';
END; $$;

CREATE FUNCTION public.coinpup_notify_platform_transfer() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog AS $$
BEGIN
    PERFORM pg_advisory_xact_lock(18945999704708432);
    INSERT INTO public.change_log(owner_id, entity_type, entity_id, entity_version, change_kind)
        VALUES (NEW.owner_id, 'platform_transfers', NEW.id::text, NEW.version,
                CASE WHEN NEW.version = 1 THEN 'upsert' ELSE 'cancel' END);
    RETURN NEW;
END; $$;

CREATE FUNCTION public.coinpup_guard_platform_line() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog AS $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM public.journals header
        JOIN public.financial_operations operation
          ON (operation.id, operation.ledger_id) = (header.operation_id, header.ledger_id)
        WHERE (header.id, header.ledger_id) = (NEW.journal_id, NEW.ledger_id)
          AND operation.kind = 'platform_move'
    ) THEN
        RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'Platform clearing needs its own group',
            CONSTRAINT = 'ck_platform_clearing_scope';
    END IF;
    RETURN NEW;
END; $$;

CREATE FUNCTION public.coinpup_validate_platform_posting(identifier uuid, common_result jsonb)
RETURNS void LANGUAGE plpgsql SET search_path = pg_catalog AS $$
DECLARE header public.journals%ROWTYPE; item public.platform_transfers%ROWTYPE;
        source_member boolean; same_ledger boolean; destination_line integer;
BEGIN
    SELECT * INTO STRICT header FROM public.journals WHERE id = identifier;
    SELECT * INTO STRICT item FROM public.platform_transfers
    WHERE header.operation_id IN (source_operation_id, destination_operation_id);
    source_member := header.operation_id = item.source_operation_id;
    same_ledger := item.destination_operation_id IS NULL;
    destination_line := CASE WHEN same_ledger THEN 3 ELSE 1 END;
    IF header.operation_version <> 1
       OR header.transaction_date <> item.transaction_date
       OR header.recognition_date <> item.transaction_date
       OR header.description <> item.description
       OR (NOT source_member AND EXISTS (
            SELECT 1 FROM public.journal_lines WHERE journal_id = identifier AND component_no > 0
       ))
       OR EXISTS (
            SELECT 1 FROM public.journal_lines WHERE journal_id = identifier
              AND num_nonnulls(party_id, counterparty_entity_id, dimension_owner_id,
                               project_id, document_id, document_line_id) <> 0
       )
    THEN
        RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'Platform journal facts disagree',
            CONSTRAINT = 'ck_platform_journal_shape';
    END IF;
    IF common_result->>'error_message' IS NOT NULL THEN
        RAISE EXCEPTION USING ERRCODE = common_result->>'error_sqlstate',
            MESSAGE = common_result->>'error_message',
            CONSTRAINT = common_result->>'error_constraint';
    END IF;
    IF EXISTS (
        WITH expected(line_no, role, asset_id, account_id, amount) AS (
            SELECT 1, 'account', item.source_asset_id, item.source_account_id, -item.amount
            WHERE source_member
            UNION ALL
            SELECT 2, 'platform_clear', item.source_asset_id, NULL, item.amount
            WHERE source_member
            UNION ALL
            SELECT destination_line, 'account', item.destination_asset_id,
                   item.destination_account_id, item.amount
            WHERE same_ledger OR NOT source_member
            UNION ALL
            SELECT destination_line + 1, 'platform_clear', item.destination_asset_id,
                   NULL, -item.amount
            WHERE same_ledger OR NOT source_member
        ), actual AS (
            SELECT line_no, role, asset_id, account_id, amount FROM public.journal_lines
            WHERE journal_id = identifier AND component_no = 0
        )
        (TABLE expected EXCEPT TABLE actual)
        UNION ALL
        (TABLE actual EXCEPT TABLE expected)
    ) THEN
        RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'Platform clearing does not match',
            CONSTRAINT = 'ck_platform_journal_shape';
    END IF;
END; $$;

CREATE FUNCTION public.coinpup_validate_platform_transfer(identifier uuid) RETURNS void
LANGUAGE plpgsql SET search_path = pg_catalog AS $$
DECLARE item public.platform_transfers%ROWTYPE;
        source_scale integer; destination_scale integer; source_journal uuid; member_journal uuid;
        expected_post jsonb; expected_cancel jsonb; postings jsonb; reversals jsonb;
        fee_rows jsonb; cancellation_reason text; expected_status text;
BEGIN
    SELECT * INTO STRICT item FROM public.platform_transfers WHERE id = identifier;
    expected_status := CASE WHEN item.version = 1 THEN 'active' ELSE 'cancelled' END;
    IF EXISTS (
        SELECT 1 FROM public.financial_operations operation
        WHERE operation.id IN (item.source_operation_id, item.destination_operation_id)
          AND (operation.kind <> 'platform_move' OR operation.created_by <> item.owner_id
               OR operation.version <> item.version OR operation.status <> expected_status)
    ) OR (SELECT count(*) FROM public.financial_operations operation
          WHERE operation.id IN (item.source_operation_id, item.destination_operation_id)) <>
          (CASE WHEN item.destination_operation_id IS NULL THEN 1 ELSE 2 END)
       OR EXISTS (
           SELECT 1 FROM public.platform_transfers other
           WHERE other.id <> item.id AND (
               other.source_operation_id IN (item.source_operation_id, item.destination_operation_id)
               OR other.destination_operation_id IN (
                   item.source_operation_id, item.destination_operation_id)
           )
       )
    THEN
        RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'Platform members must commit together',
            CONSTRAINT = 'ck_platform_transfer_members';
    END IF;

    -- Already sealed operations might otherwise be attached via the other membership column.
    FOR member_journal IN
        SELECT current_journal_id FROM public.financial_operations
        WHERE id IN (item.source_operation_id, item.destination_operation_id)
    LOOP
        PERFORM public.coinpup_validate_platform_posting(
            member_journal, public.coinpup_validate_journal_common(member_journal));
    END LOOP;

    SELECT asset.scale INTO STRICT source_scale FROM public.assets asset
    WHERE asset.asset_id = item.source_asset_id;
    SELECT asset.scale INTO STRICT destination_scale FROM public.assets asset
    WHERE asset.asset_id = item.destination_asset_id;
    SELECT operation.current_journal_id INTO STRICT source_journal
    FROM public.financial_operations operation WHERE operation.id = item.source_operation_id;

    SELECT jsonb_agg(jsonb_build_object('ledger_id', ledger_id, 'operation_id', id,
                  'journal_id', current_journal_id) ORDER BY id <> item.source_operation_id)
    INTO postings
    FROM public.financial_operations
    WHERE id IN (item.source_operation_id, item.destination_operation_id);

    SELECT coalesce(jsonb_agg(jsonb_build_object(
        'account_id', debit.account_id, 'asset_id', debit.asset_id,
        'category_id', expense.category_id, 'amount', round(-debit.amount, asset.scale)::text
    ) ORDER BY debit.component_no), '[]'::jsonb)
    INTO fee_rows
    FROM public.journal_lines debit
    JOIN public.journal_lines expense
      ON (expense.journal_id, expense.component_no) = (debit.journal_id, debit.component_no)
    JOIN public.assets asset ON asset.asset_id = debit.asset_id
    WHERE debit.journal_id = source_journal AND debit.component_no > 0
      AND debit.role = 'account' AND expense.role = 'expense';

    expected_post := jsonb_build_object(
        'id', item.id, 'kind', item.kind, 'source_ledger_id', item.source_ledger_id,
        'source_account_id', item.source_account_id, 'source_asset_id', item.source_asset_id,
        'source_amount', round(item.amount, source_scale)::text,
        'destination_ledger_id', item.destination_ledger_id,
        'destination_account_id', item.destination_account_id,
        'destination_asset_id', item.destination_asset_id,
        'destination_amount', round(item.amount, destination_scale)::text,
        'transaction_date', to_char(item.transaction_date, 'YYYY-MM-DD'),
        'description', item.description, 'fees', fee_rows, 'operations', postings,
        'status', 'active', 'version', 1
    );
    IF NOT EXISTS (
        SELECT 1 FROM public.command_receipts receipt
        WHERE (receipt.ledger_id, receipt.key) = (item.source_ledger_id, item.create_key)
          AND receipt.operation_id = item.source_operation_id AND receipt.hash_version = 2
          AND receipt.response_status = 201 AND receipt.response = expected_post
    ) THEN
        RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'Platform create receipt differs',
            CONSTRAINT = 'ck_platform_transfer_receipt';
    END IF;

    IF item.version = 2 THEN
        SELECT jsonb_agg(jsonb_build_object('ledger_id', ledger_id, 'operation_id', operation_id,
                         'journal_id', id) ORDER BY ledger_id, id),
               min(revision_reason)
        INTO reversals, cancellation_reason
        FROM public.journals
        WHERE operation_id IN (item.source_operation_id, item.destination_operation_id)
          AND journal_kind = 'reversal' AND operation_version = 2
        HAVING count(*) = (CASE WHEN item.destination_operation_id IS NULL THEN 1 ELSE 2 END)
           AND count(DISTINCT revision_reason) = 1;
        expected_cancel := jsonb_build_object(
            'id', item.id, 'source_ledger_id', item.source_ledger_id, 'status', 'cancelled',
            'version', 2, 'reason', cancellation_reason, 'reversals', reversals
        );
        IF reversals IS NULL OR NOT EXISTS (
            SELECT 1 FROM public.command_receipts receipt
            WHERE (receipt.ledger_id, receipt.key) = (item.source_ledger_id, item.cancel_key)
              AND receipt.operation_id = item.source_operation_id AND receipt.hash_version = 2
              AND receipt.response_status = 200 AND receipt.response = expected_cancel
        ) THEN
            RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'Platform reversal group differs',
                CONSTRAINT = 'ck_platform_transfer_receipt';
        END IF;
    END IF;
END; $$;

CREATE FUNCTION public.coinpup_check_platform_transfer() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog AS $$
BEGIN
    PERFORM public.coinpup_validate_platform_transfer(NEW.id);
    RETURN NEW;
END; $$;

CREATE FUNCTION public.coinpup_check_platform_member() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog AS $$
DECLARE identifier uuid;
BEGIN
    SELECT id INTO STRICT identifier FROM public.platform_transfers
    WHERE NEW.id IN (source_operation_id, destination_operation_id);
    PERFORM public.coinpup_validate_platform_transfer(identifier);
    RETURN NEW;
END; $$;

CREATE TRIGGER tr_platform_transfers_guard BEFORE INSERT OR UPDATE OR DELETE
ON public.platform_transfers FOR EACH ROW EXECUTE FUNCTION public.coinpup_guard_platform_transfer();

CREATE TRIGGER tr_platform_transfers_change AFTER INSERT OR UPDATE
ON public.platform_transfers FOR EACH ROW EXECUTE FUNCTION public.coinpup_notify_platform_transfer();

CREATE TRIGGER tr_journal_lines_platform_clearing BEFORE INSERT ON public.journal_lines
FOR EACH ROW WHEN (NEW.role = 'platform_clear') EXECUTE FUNCTION public.coinpup_guard_platform_line();

CREATE CONSTRAINT TRIGGER zz_tr_platform_transfers_complete AFTER INSERT OR UPDATE
ON public.platform_transfers DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION public.coinpup_check_platform_transfer();

CREATE CONSTRAINT TRIGGER zz_tr_platform_members_complete AFTER INSERT OR UPDATE
ON public.financial_operations DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW WHEN (NEW.kind = 'platform_move') EXECUTE FUNCTION public.coinpup_check_platform_member();
