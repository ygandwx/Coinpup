"""Permanent reminder email identities, attempts, and ordered evidence notifications."""

from pathlib import Path

from alembic import op

revision = "20261125_0108"
down_revision = "20261125_0107"
branch_labels = None
depends_on = None

_SQL = Path(__file__).with_name("sql") / "0108"
_OLD_TYPES = (
    "'entities', 'ledgers', 'accounts', 'account_assets', 'categories', 'assets', "
    "'financial_operations', 'stored_files', 'operation_file_links', "
    "'ocr_jobs', 'ocr_drafts', 'ocr_confirmations', 'business_parties', 'business_documents', "
    "'business_document_lines', 'ledger_periods', 'ledger_period_audits', "
    "'ledger_period_receipts', 'business_projects', 'recurring_invoice_rules', "
    "'recurring_invoice_instances', 'reminder_events', 'reminder_event_revisions', "
    "'reminder_delivery_configs', 'reminder_delivery_revisions', 'business_confirmations', "
    "'business_postings', 'business_payments', 'business_allocations', "
    "'business_allocation_lines', 'business_allocation_returns', "
    "'business_allocation_return_lines', 'business_refunds', 'business_refund_returns', "
    "'business_credits', 'business_credit_lines', 'business_credit_revisions', "
    "'business_credit_revision_members', 'business_refund_revisions', "
    "'business_refund_revision_returns', 'business_return_revisions', "
    "'business_return_revision_members', 'business_document_revisions', "
    "'business_document_revision_lines', 'business_document_accruals', "
    "'business_invoice_profiles', 'business_invoice_reservations', "
    "'business_invoice_completions', 'interledger_settings', 'interledger_groups', 'manual_prices'"
)

_ENTITY_TYPES = _OLD_TYPES + ", 'reminder_email_deliveries', 'reminder_email_evidence'"


def _install_change_types(types):
    op.drop_constraint("ck_change_log_entity_type", "change_log", type_="check")
    op.create_check_constraint(
        "ck_change_log_entity_type", "change_log", f"entity_type IN ({types})"
    )


def upgrade():
    op.execute("SELECT pg_advisory_xact_lock(18945999704708432)")
    _install_change_types(_ENTITY_TYPES)
    op.execute((_SQL / "reminder_email.sql").read_text(encoding="utf-8"))


def downgrade():
    op.execute("SELECT pg_advisory_xact_lock(18945999704708432)")
    op.execute("""
        LOCK TABLE public.reminder_email_deliveries, public.reminder_email_evidence
            IN ACCESS EXCLUSIVE MODE;
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM public.reminder_email_deliveries)
                OR EXISTS (SELECT 1 FROM public.reminder_email_evidence)
                OR EXISTS (SELECT 1 FROM public.change_log
                    WHERE entity_type IN (
                        'reminder_email_deliveries','reminder_email_evidence')) THEN
                RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='Reminder email history exists',
                    CONSTRAINT='ck_reminder_email_downgrade';
            END IF;
        END; $$;
    """)
    op.drop_table("reminder_email_evidence")
    op.drop_table("reminder_email_deliveries")
    for function in (
        "coinpup_guard_reminder_email_identity",
        "coinpup_guard_reminder_email_evidence",
        "coinpup_notify_reminder_email",
        "coinpup_check_reminder_email_claim",
    ):
        op.execute(f"DROP FUNCTION public.{function}()")
    _install_change_types(_OLD_TYPES)
