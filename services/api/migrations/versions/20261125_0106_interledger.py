"""Atomic same-asset advances and repayments between one owner's ledgers."""

from pathlib import Path

from alembic import op

revision = "20261125_0106"
down_revision = "20261114_0105"
branch_labels = None
depends_on = None

_SQL = Path(__file__).with_name("sql") / "0106"
_OLD_KINDS = (
    "'opening', 'income', 'expense', 'transfer', 'exchange', "
    "'invoice_accrual', 'bill_accrual', 'payment', 'allocation', 'unallocation', 'refund', "
    "'invoice_credit', 'bill_credit'"
)
_INTERLEDGER_KINDS = _OLD_KINDS + ", 'advance', 'repayment'"
_OLD_TYPES = (
    "'entities', 'ledgers', 'accounts', 'account_assets', 'categories', 'assets', "
    "'financial_operations', 'stored_files', 'operation_file_links', "
    "'ocr_jobs', 'ocr_drafts', 'ocr_confirmations', 'business_parties', 'business_documents', "
    "'business_document_lines', 'ledger_periods', 'ledger_period_audits', "
    "'ledger_period_receipts', "
    "'business_projects', 'recurring_invoice_rules', 'recurring_invoice_instances', "
    "'reminder_events', 'reminder_event_revisions', 'reminder_delivery_configs', "
    "'reminder_delivery_revisions', 'business_confirmations', 'business_postings', "
    "'business_payments', 'business_allocations', 'business_allocation_lines', "
    "'business_allocation_returns', 'business_allocation_return_lines', "
    "'business_refunds', 'business_refund_returns', 'business_credits', 'business_credit_lines', "
    "'business_credit_revisions', 'business_credit_revision_members', 'business_refund_revisions', "
    "'business_refund_revision_returns', 'business_return_revisions', "
    "'business_return_revision_members', 'business_document_revisions', "
    "'business_document_revision_lines', 'business_document_accruals', "
    "'business_invoice_profiles', 'business_invoice_reservations', 'business_invoice_completions'"
)


def _install_operation_kinds(kinds):
    op.drop_constraint("ck_financial_operations_kind", "financial_operations", type_="check")
    op.create_check_constraint(
        "ck_financial_operations_kind", "financial_operations", f"kind IN ({kinds})"
    )


def _install_checks(kinds, types):
    _install_operation_kinds(kinds)
    op.drop_constraint("ck_change_log_entity_type", "change_log", type_="check")
    op.create_check_constraint(
        "ck_change_log_entity_type", "change_log", f"entity_type IN ({types})"
    )


def _install_interledger_dispatch():
    before = (_SQL / "prior_dispatch.sql").read_text(encoding="utf-8")
    boundary = "        ELSE RAISE EXCEPTION"
    if before.count(boundary) != 1:
        raise RuntimeError("Expected one frozen posting dispatcher fallback")
    after = before.replace(
        boundary,
        "        WHEN 'advance', 'repayment' THEN\n"
        "            PERFORM public.coinpup_validate_interledger_journal(journal_identifier);\n"
        + boundary,
    )
    op.execute(after)


def upgrade():
    op.execute("SELECT pg_advisory_xact_lock(18945999704708432)")
    _install_checks(
        _INTERLEDGER_KINDS,
        _OLD_TYPES + ", 'interledger_settings', 'interledger_groups'",
    )
    op.execute((_SQL / "interledger.sql").read_text(encoding="utf-8"))
    _install_interledger_dispatch()


def downgrade():
    op.execute("SELECT pg_advisory_xact_lock(18945999704708432)")
    op.execute("""
        LOCK TABLE public.interledger_settings, public.interledger_groups IN ACCESS EXCLUSIVE MODE;
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM public.interledger_settings)
                OR EXISTS (SELECT 1 FROM public.interledger_groups)
                OR EXISTS (SELECT 1 FROM public.change_log
                    WHERE entity_type IN ('interledger_settings', 'interledger_groups')) THEN
                RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='Interledger history exists',
                    CONSTRAINT='ck_interledger_downgrade';
            END IF;
        END; $$;
    """)
    op.execute((_SQL / "prior_dispatch.sql").read_text(encoding="utf-8"))
    op.execute("DROP TRIGGER tr_interledger_operations_guard ON public.financial_operations")
    op.drop_table("interledger_groups")
    op.drop_table("interledger_settings")
    for name, signature in (
        ("coinpup_validate_interledger_pair", ""),
        ("coinpup_validate_interledger_journal", "uuid"),
        ("coinpup_guard_interledger_operation", ""),
        ("coinpup_guard_interledger_group", ""),
        ("coinpup_guard_interledger_settings", ""),
        ("coinpup_notify_interledger", ""),
    ):
        op.execute(f"DROP FUNCTION public.{name}({signature})")
    _install_checks(_OLD_KINDS, _OLD_TYPES)
