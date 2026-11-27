DROP TRIGGER zz_tr_platform_members_complete ON public.financial_operations;
DROP TRIGGER zz_tr_platform_transfers_complete ON public.platform_transfers;
DROP TRIGGER tr_journal_lines_platform_clearing ON public.journal_lines;
DROP TRIGGER tr_platform_transfers_change ON public.platform_transfers;
DROP TRIGGER tr_platform_transfers_guard ON public.platform_transfers;
DROP TRIGGER tr_accounts_custody ON public.accounts;
DROP TRIGGER tr_account_assets_custody ON public.account_assets;

DROP FUNCTION public.coinpup_check_platform_member();
DROP FUNCTION public.coinpup_check_platform_transfer();
DROP FUNCTION public.coinpup_validate_platform_transfer(uuid);
DROP FUNCTION public.coinpup_validate_platform_posting(uuid, jsonb);
DROP FUNCTION public.coinpup_guard_platform_line();
DROP FUNCTION public.coinpup_notify_platform_transfer();
DROP FUNCTION public.coinpup_guard_platform_transfer();
DROP FUNCTION public.coinpup_validate_custody_mode();
DROP FUNCTION public.coinpup_validate_custody_link();
DROP FUNCTION public.coinpup_custody_link_allowed(text, text, text);
DROP FUNCTION public.coinpup_custody_family(text);
