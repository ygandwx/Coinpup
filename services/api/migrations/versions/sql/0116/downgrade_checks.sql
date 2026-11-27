ALTER TABLE public.assets DROP CONSTRAINT ck_assets_identity;

ALTER TABLE public.assets ADD CONSTRAINT ck_assets_identity CHECK (

((kind = 'fiat' AND code ~ '^[A-Z]{3}$' AND code NOT IN ('BTC', 'ETH', 'XMR')
  AND (code NOT IN ('USD', 'GBP', 'EUR', 'HKD', 'CNY') OR scale = 2)
  AND network IS NULL AND token_reference IS NULL AND asset_id = code) OR
(kind = 'native' AND token_reference IS NULL AND network IS NOT NULL
 AND asset_id = code
 AND ((code = 'BTC' AND network = 'bitcoin' AND scale = 8)
       OR (code = 'ETH' AND network = 'ethereum' AND scale = 18)
       OR (code = 'XMR' AND network = 'monero' AND scale = 12))) OR
(kind = 'native' AND token_reference IS NULL AND network IS NOT NULL
 AND asset_id = 'native:' || network || ':' || code
 AND ((code = 'SOL' AND network = 'solana' AND scale = 9)
       OR (code = 'BNB' AND network = 'bsc' AND scale = 18)
       OR (code = 'TRX' AND network = 'tron' AND scale = 6)
       OR (code = 'XRP' AND network = 'xrpl' AND scale = 6)
       OR (code = 'DOGE' AND network = 'dogecoin' AND scale = 8)
       OR (code = 'LTC' AND network = 'litecoin' AND scale = 8)
       OR (code = 'ADA' AND network = 'cardano' AND scale = 6)
       OR (code = 'TON' AND network = 'ton' AND scale = 9)
       OR (code = 'AVAX' AND network = 'avalanche' AND scale = 18)
       OR (code = 'DOT' AND network = 'polkadot' AND scale = 10)
       OR (code = 'POL' AND network = 'polygon' AND scale = 18))) OR
(kind = 'token' AND code ~ '^[A-Z0-9]{2,10}$'
 AND network IS NOT NULL AND network ~ '^[a-z][a-z0-9_-]{0,63}$'
 AND token_reference IS NOT NULL
 AND token_reference ~ '^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$'
 AND asset_id = 'token:' || network || ':' || token_reference))

);

ALTER TABLE public.financial_operations DROP CONSTRAINT ck_financial_operations_kind;

ALTER TABLE public.financial_operations ADD CONSTRAINT ck_financial_operations_kind CHECK (
kind IN ('opening', 'income', 'expense', 'transfer', 'exchange', 'invoice_accrual', 'bill_accrual', 'payment', 'allocation', 'unallocation', 'refund', 'invoice_credit', 'bill_credit', 'advance', 'repayment')
);

ALTER TABLE public.journal_lines DROP CONSTRAINT ck_journal_lines_role;

ALTER TABLE public.journal_lines ADD CONSTRAINT ck_journal_lines_role CHECK (
(role = 'account' AND account_id IS NOT NULL AND category_id IS NULL) OR (role IN ('income', 'expense') AND account_id IS NULL AND category_id IS NOT NULL) OR (role IN ('equity', 'exchange') AND account_id IS NULL AND category_id IS NULL)
);

ALTER TABLE public.change_log DROP CONSTRAINT ck_change_log_entity_type;

ALTER TABLE public.change_log ADD CONSTRAINT ck_change_log_entity_type CHECK (
entity_type IN ('entities', 'ledgers', 'accounts', 'account_assets', 'categories', 'assets', 'financial_operations', 'stored_files', 'operation_file_links', 'ocr_jobs', 'ocr_drafts', 'ocr_confirmations', 'business_parties', 'business_documents', 'business_document_lines', 'ledger_periods', 'ledger_period_audits', 'ledger_period_receipts', 'business_projects', 'recurring_invoice_rules', 'recurring_invoice_instances', 'reminder_events', 'reminder_event_revisions', 'reminder_delivery_configs', 'reminder_delivery_revisions', 'business_confirmations', 'business_postings', 'business_payments', 'business_allocations', 'business_allocation_lines', 'business_allocation_returns', 'business_allocation_return_lines', 'business_refunds', 'business_refund_returns', 'business_credits', 'business_credit_lines', 'business_credit_revisions', 'business_credit_revision_members', 'business_refund_revisions', 'business_refund_revision_returns', 'business_return_revisions', 'business_return_revision_members', 'business_document_revisions', 'business_document_revision_lines', 'business_document_accruals', 'business_invoice_profiles', 'business_invoice_reservations', 'business_invoice_completions', 'interledger_settings', 'interledger_groups', 'manual_prices', 'reminder_email_deliveries', 'reminder_email_evidence', 'quote_observations', 'reminder_inbox_deliveries')
);
