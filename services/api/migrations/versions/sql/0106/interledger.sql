CREATE TABLE public.interledger_settings (
    owner_id uuid PRIMARY KEY,
    enabled boolean NOT NULL,
    version integer NOT NULL,
    CONSTRAINT fk_interledger_settings_owner FOREIGN KEY(owner_id)
        REFERENCES public.administrators(id) ON DELETE RESTRICT,
    CONSTRAINT ck_interledger_settings_version CHECK(version > 0)
);

CREATE TABLE public.interledger_groups (
    id uuid PRIMARY KEY,
    owner_id uuid NOT NULL,
    kind varchar(16) NOT NULL,
    payer_ledger_id uuid NOT NULL,
    beneficiary_ledger_id uuid NOT NULL,
    payer_account_id uuid NOT NULL,
    beneficiary_account_id uuid,
    expense_category_id uuid,
    expense_kind varchar(8),
    advance_id uuid,
    advance_kind varchar(16),
    payer_operation_id uuid NOT NULL,
    beneficiary_operation_id uuid NOT NULL,
    asset_id varchar(200) NOT NULL,
    amount numeric(38,18) NOT NULL,
    transaction_date date NOT NULL,
    description varchar(2000) NOT NULL,
    command_key varchar(128) NOT NULL,
    request_hash varchar(64) NOT NULL,
    response jsonb NOT NULL,
    CONSTRAINT uq_interledger_owner_command UNIQUE(owner_id, command_key),
    CONSTRAINT uq_interledger_payer_operation UNIQUE(payer_operation_id),
    CONSTRAINT uq_interledger_beneficiary_operation UNIQUE(beneficiary_operation_id),
    CONSTRAINT uq_interledger_source_identity
        UNIQUE(id, owner_id, payer_ledger_id, beneficiary_ledger_id, asset_id, kind),
    CONSTRAINT fk_interledger_payer_owner FOREIGN KEY(payer_ledger_id, owner_id)
        REFERENCES public.ledgers(id, owner_id) ON DELETE RESTRICT,
    CONSTRAINT fk_interledger_beneficiary_owner FOREIGN KEY(beneficiary_ledger_id, owner_id)
        REFERENCES public.ledgers(id, owner_id) ON DELETE RESTRICT,
    CONSTRAINT fk_interledger_payer_operation FOREIGN KEY(payer_operation_id, payer_ledger_id)
        REFERENCES public.financial_operations(id, ledger_id) ON DELETE RESTRICT,
    CONSTRAINT fk_interledger_beneficiary_operation
        FOREIGN KEY(beneficiary_operation_id, beneficiary_ledger_id)
        REFERENCES public.financial_operations(id, ledger_id) ON DELETE RESTRICT,
    CONSTRAINT fk_interledger_payer_account FOREIGN KEY(payer_account_id, asset_id, payer_ledger_id)
        REFERENCES public.account_assets(account_id, asset_id, ledger_id) ON DELETE RESTRICT,
    CONSTRAINT fk_interledger_beneficiary_account
        FOREIGN KEY(beneficiary_account_id, asset_id, beneficiary_ledger_id)
        REFERENCES public.account_assets(account_id, asset_id, ledger_id) ON DELETE RESTRICT,
    CONSTRAINT fk_interledger_expense_category
        FOREIGN KEY(expense_category_id, beneficiary_ledger_id, expense_kind)
        REFERENCES public.categories(id, ledger_id, kind) ON DELETE RESTRICT,
    CONSTRAINT fk_interledger_advance
        FOREIGN KEY(advance_id, owner_id, payer_ledger_id, beneficiary_ledger_id, asset_id, advance_kind)
        REFERENCES public.interledger_groups(id, owner_id, payer_ledger_id, beneficiary_ledger_id, asset_id, kind)
        ON DELETE RESTRICT,
    CONSTRAINT ck_interledger_pair CHECK(payer_ledger_id <> beneficiary_ledger_id),
    CONSTRAINT ck_interledger_shape CHECK(
        (kind = 'advance' AND advance_id IS NULL AND advance_kind IS NULL
            AND beneficiary_account_id IS NULL AND expense_category_id IS NOT NULL
            AND expense_kind IS NOT DISTINCT FROM 'expense') OR
        (kind = 'repayment' AND advance_id IS NOT NULL AND advance_id <> id
            AND advance_kind IS NOT DISTINCT FROM 'advance' AND beneficiary_account_id IS NOT NULL
            AND expense_category_id IS NULL AND expense_kind IS NULL)
    ),
    CONSTRAINT ck_interledger_amount CHECK(amount > 0 AND amount < 100000000000000000000),
    CONSTRAINT ck_interledger_key CHECK(command_key ~ '^[!-~]{1,128}$'),
    CONSTRAINT ck_interledger_request_hash CHECK(request_hash ~ '^[0-9a-f]{64}$'),
    CONSTRAINT ck_interledger_response CHECK(jsonb_typeof(response) = 'object')
);

CREATE INDEX ix_interledger_owner_date ON public.interledger_groups(owner_id, transaction_date, id);
CREATE INDEX ix_interledger_advance_date ON public.interledger_groups(advance_id, transaction_date, id);

CREATE FUNCTION public.coinpup_notify_interledger() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog AS $$
BEGIN
    PERFORM pg_advisory_xact_lock(18945999704708432);
    IF TG_TABLE_NAME = 'interledger_settings' THEN
        INSERT INTO public.change_log(owner_id, entity_type, entity_id, entity_version, change_kind)
            VALUES (NEW.owner_id, TG_TABLE_NAME, NEW.owner_id::text, NEW.version, 'upsert');
    ELSE
        INSERT INTO public.change_log(owner_id, entity_type, entity_id, entity_version, change_kind)
            VALUES (NEW.owner_id, TG_TABLE_NAME, NEW.id::text, 1, 'upsert');
    END IF;
    RETURN NEW;
END; $$;

CREATE FUNCTION public.coinpup_guard_interledger_settings() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog AS $$
BEGIN
    IF ((TG_OP = 'INSERT' AND NEW.version = 1 AND NEW.enabled) OR
        (TG_OP = 'UPDATE' AND NEW.owner_id = OLD.owner_id AND NEW.version = OLD.version + 1
            AND NEW.enabled IS DISTINCT FROM OLD.enabled)) THEN
        RETURN NEW;
    END IF;
    RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'Invalid interledger setting transition',
        CONSTRAINT = 'ck_interledger_settings_history';
END; $$;

CREATE FUNCTION public.coinpup_guard_interledger_group() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog AS $$
DECLARE source public.interledger_groups%ROWTYPE;
        repaid numeric; asset_scale integer;
        expected_response jsonb; quantity_text text;
BEGIN
    IF TG_OP <> 'INSERT' THEN
        RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'Interledger groups are immutable',
            CONSTRAINT = 'ck_interledger_immutable';
    END IF;
    -- A writer waiting behind the shared change lock must see newly committed repayments.
    IF current_setting('transaction_isolation') <> 'read committed' THEN
        RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'Interledger requires read committed',
            CONSTRAINT = 'ck_interledger_write_isolation';
    END IF;
    PERFORM pg_advisory_xact_lock(18945999704708432);
    IF NOT EXISTS (SELECT 1 FROM public.interledger_settings
        WHERE owner_id = NEW.owner_id AND enabled) THEN
        RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'Interledger is disabled',
            CONSTRAINT = 'ck_interledger_enabled';
    END IF;
    SELECT scale INTO asset_scale FROM public.assets WHERE asset_id = NEW.asset_id;
    IF NEW.amount IS DISTINCT FROM round(NEW.amount, asset_scale) THEN
        RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'Invalid interledger precision',
            CONSTRAINT = 'ck_interledger_precision';
    END IF;
    quantity_text := round(NEW.amount, asset_scale)::text;
    expected_response := jsonb_build_object(
        'id', NEW.id, 'amount', quantity_text,
        'transaction_date', to_char(NEW.transaction_date, 'YYYY-MM-DD'),
        'description', NEW.description, 'payer_operation_id', NEW.payer_operation_id,
        'beneficiary_operation_id', NEW.beneficiary_operation_id
    );
    IF NEW.kind = 'advance' THEN
        expected_response := expected_response || jsonb_build_object(
            'payer_ledger_id', NEW.payer_ledger_id, 'beneficiary_ledger_id', NEW.beneficiary_ledger_id,
            'payer_account_id', NEW.payer_account_id, 'expense_category_id', NEW.expense_category_id,
            'asset_id', NEW.asset_id, 'outstanding_amount', quantity_text
        );
    ELSE
        expected_response := expected_response || jsonb_build_object(
            'advance_id', NEW.advance_id, 'payer_receipt_account_id', NEW.payer_account_id,
            'beneficiary_payment_account_id', NEW.beneficiary_account_id
        );
    END IF;
    IF NEW.response IS DISTINCT FROM expected_response THEN
        RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'Interledger receipt differs from its group',
            CONSTRAINT = 'ck_interledger_receipt_facts';
    END IF;
    IF NEW.kind = 'repayment' THEN
        SELECT * INTO source FROM public.interledger_groups WHERE id = NEW.advance_id FOR UPDATE;
        SELECT coalesce(sum(amount), 0) INTO repaid FROM public.interledger_groups
            WHERE advance_id = NEW.advance_id;
        IF source.id IS NULL OR NEW.transaction_date < source.transaction_date
            OR NEW.amount + repaid > source.amount THEN
            RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'Invalid interledger repayment',
                CONSTRAINT = 'ck_interledger_capacity';
        END IF;
    END IF;
    RETURN NEW;
END; $$;

CREATE FUNCTION public.coinpup_guard_interledger_operation() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog AS $$
BEGIN
    IF OLD.kind IN ('advance', 'repayment') THEN
        RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'Linked operations are immutable',
            CONSTRAINT = 'ck_interledger_immutable';
    END IF;
    IF TG_OP = 'DELETE' THEN RETURN OLD; END IF;
    RETURN NEW;
END; $$;

CREATE FUNCTION public.coinpup_validate_interledger_journal(journal_identifier uuid) RETURNS void
LANGUAGE plpgsql SET search_path = pg_catalog AS $$
DECLARE header public.journals%ROWTYPE; operation public.financial_operations%ROWTYPE;
        pair public.interledger_groups%ROWTYPE; matches integer; payer_side boolean;
        principal numeric; principal_account uuid; counterparty uuid; system_account text;
BEGIN
    SELECT * INTO header FROM public.journals WHERE id = journal_identifier;
    SELECT * INTO operation FROM public.financial_operations WHERE id = header.operation_id;
    IF (SELECT count(*) FROM public.interledger_groups
        WHERE payer_operation_id = operation.id OR beneficiary_operation_id = operation.id) <> 1 THEN
        RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'Interledger operation has no unique group',
            CONSTRAINT = 'ck_interledger_operation_link';
    END IF;
    SELECT * INTO pair FROM public.interledger_groups
        WHERE payer_operation_id = operation.id OR beneficiary_operation_id = operation.id;
    payer_side := pair.payer_operation_id = operation.id;
    principal := CASE WHEN payer_side = (pair.kind = 'advance') THEN -pair.amount ELSE pair.amount END;
    principal_account := CASE WHEN payer_side THEN pair.payer_account_id ELSE pair.beneficiary_account_id END;
    system_account := CASE WHEN payer_side THEN 'intercompany.receivable' ELSE 'intercompany.payable' END;
    SELECT entity_id INTO counterparty FROM public.ledgers
        WHERE id = CASE WHEN payer_side THEN pair.beneficiary_ledger_id ELSE pair.payer_ledger_id END;
    IF operation.kind IS DISTINCT FROM pair.kind OR operation.version <> 1 OR
        operation.status <> 'active' OR operation.created_by IS DISTINCT FROM pair.owner_id OR
        operation.current_journal_id IS DISTINCT FROM journal_identifier OR
        header.journal_kind <> 'posting' OR header.reverses_journal_id IS NOT NULL OR
        header.transaction_date IS DISTINCT FROM pair.transaction_date OR
        header.recognition_date IS DISTINCT FROM pair.transaction_date OR
        header.description IS DISTINCT FROM pair.description THEN
        RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'Interledger journal identity differs',
            CONSTRAINT = 'ck_interledger_journal_shape';
    END IF;
    SELECT count(*) INTO matches FROM public.journal_lines line
        LEFT JOIN public.accounts account ON account.id = line.account_id
        WHERE line.journal_id = header.id AND line.ledger_id = header.ledger_id
        AND line.component_no = 0 AND line.asset_id = pair.asset_id
        AND line.project_id IS NULL AND line.party_id IS NULL
        AND line.document_id IS NULL AND line.document_line_id IS NULL
        AND (
            (line.line_no = 1 AND line.amount = principal AND line.counterparty_entity_id IS NULL
                AND ((principal_account IS NULL AND line.role = 'expense'
                        AND line.category_id = pair.expense_category_id)
                    OR (principal_account IS NOT NULL AND line.role = 'account'
                        AND line.account_id = principal_account AND account.account_class = 'money')))
            OR
            (line.line_no = 2 AND line.role = 'account' AND line.amount = -principal
                AND account.system_key = system_account AND line.counterparty_entity_id = counterparty
                AND line.dimension_owner_id = pair.owner_id)
        );
    IF matches <> 2 OR (SELECT count(*) FROM public.journal_lines
        WHERE journal_id = header.id) <> 2 THEN
        RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'Interledger journal lines differ',
            CONSTRAINT = 'ck_interledger_journal_shape';
    END IF;
END; $$;

CREATE FUNCTION public.coinpup_validate_interledger_pair() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog AS $$
BEGIN
    IF (SELECT count(*) FROM public.financial_operations
        WHERE id IN (NEW.payer_operation_id, NEW.beneficiary_operation_id)
        AND kind = NEW.kind) <> 2 THEN
        RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'Interledger operation kinds differ',
            CONSTRAINT = 'ck_interledger_operation_link';
    END IF;
    PERFORM public.coinpup_validate_operation_history(NEW.payer_operation_id);
    PERFORM public.coinpup_validate_operation_history(NEW.beneficiary_operation_id);
    RETURN NULL;
END; $$;

CREATE TRIGGER tr_interledger_settings_guard BEFORE INSERT OR UPDATE OR DELETE
    ON public.interledger_settings FOR EACH ROW EXECUTE FUNCTION public.coinpup_guard_interledger_settings();
CREATE TRIGGER tr_interledger_settings_change AFTER INSERT OR UPDATE
    ON public.interledger_settings FOR EACH ROW EXECUTE FUNCTION public.coinpup_notify_interledger();
CREATE TRIGGER tr_interledger_groups_guard BEFORE INSERT OR UPDATE OR DELETE
    ON public.interledger_groups FOR EACH ROW EXECUTE FUNCTION public.coinpup_guard_interledger_group();
CREATE TRIGGER tr_interledger_groups_change AFTER INSERT
    ON public.interledger_groups FOR EACH ROW EXECUTE FUNCTION public.coinpup_notify_interledger();
CREATE TRIGGER tr_interledger_operations_guard BEFORE UPDATE OR DELETE
    ON public.financial_operations FOR EACH ROW EXECUTE FUNCTION public.coinpup_guard_interledger_operation();
CREATE CONSTRAINT TRIGGER zz_tr_interledger_groups_complete AFTER INSERT
    ON public.interledger_groups DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION public.coinpup_validate_interledger_pair();
