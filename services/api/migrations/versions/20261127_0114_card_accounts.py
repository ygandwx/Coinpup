"""Add independent debit-card accounts and a private number for either kind of card."""

import sqlalchemy as sa
from alembic import op

revision = "20261127_0114"
down_revision = "20261125_0113"
branch_labels = None
depends_on = None

_OLD = "'bank', 'cash', 'wechat', 'alipay', 'credit_card', 'paypal', 'wise', 'stripe', 'crypto'"
_NEW = _OLD.replace("'credit_card',", "'credit_card', 'debit_card',")


def _account_kinds(kinds):
    op.drop_constraint("ck_accounts_kind", "accounts", type_="check")
    op.create_check_constraint("ck_accounts_kind", "accounts", f"kind IN ({kinds})")


def upgrade():
    op.execute("SELECT pg_advisory_xact_lock(18945999704708432)")
    _account_kinds(_NEW)
    op.add_column("accounts", sa.Column("card_number", sa.String(19), nullable=True))
    op.add_column(
        "accounts",
        sa.Column("card_schema_used", sa.Boolean(), server_default=sa.false(), nullable=False),
    )
    op.create_check_constraint(
        "ck_accounts_card_number",
        "accounts",
        "card_number IS NULL OR (account_class = 'money' AND card_number ~ '^[0-9]{12,19}$' "
        "AND details ->> 'card_last_four' IS NOT DISTINCT FROM right(card_number, 4))",
    )
    op.execute("""
        CREATE FUNCTION public.coinpup_track_card_account_history() RETURNS trigger
        LANGUAGE plpgsql SET search_path = pg_catalog, public AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                IF OLD.card_schema_used THEN
                    RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='Card account history exists',
                        CONSTRAINT='ck_accounts_card_history';
                END IF;
                RETURN OLD;
            END IF;
            NEW.card_schema_used := NEW.kind IS NOT DISTINCT FROM 'debit_card'
                OR NEW.card_number IS NOT NULL OR
                (CASE WHEN TG_OP = 'UPDATE' THEN OLD.card_schema_used ELSE false END);
            RETURN NEW;
        END;
        $$;
        CREATE TRIGGER tr_accounts_card_history BEFORE INSERT OR UPDATE OR DELETE ON public.accounts
            FOR EACH ROW EXECUTE FUNCTION public.coinpup_track_card_account_history();
    """)


def downgrade():
    op.execute("SELECT pg_advisory_xact_lock(18945999704708432)")
    op.execute("""
        LOCK TABLE public.accounts IN ACCESS EXCLUSIVE MODE;
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM public.accounts WHERE card_schema_used
                       OR kind = 'debit_card' OR card_number IS NOT NULL)
            THEN
                RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='Card account history exists',
                    CONSTRAINT='ck_accounts_card_downgrade';
            END IF;
        END; $$;
        DROP TRIGGER tr_accounts_card_history ON public.accounts;
        DROP FUNCTION public.coinpup_track_card_account_history();
    """)
    op.drop_constraint("ck_accounts_card_number", "accounts", type_="check")
    op.drop_column("accounts", "card_schema_used")
    op.drop_column("accounts", "card_number")
    _account_kinds(_OLD)
