"""Separate custody units and preserve atomic platform transfer groups."""

from pathlib import Path

import sqlalchemy as sa
from alembic import op

revision = "20261127_0116"
down_revision = "20261127_0115"
branch_labels = None
depends_on = None

_SQL = Path(__file__).parent / "sql" / "0116"


def _read(name):
    return (_SQL / name).read_text(encoding="utf-8")


def upgrade():
    op.execute("SELECT pg_advisory_xact_lock(18945999704708432)")
    op.alter_column("assets", "kind", existing_type=sa.String(8), type_=sa.String(9))
    op.add_column("accounts", sa.Column("custody_mode", sa.String(9), nullable=True))
    op.execute(_read("upgrade_checks.sql"))
    op.execute(_read("table.sql"))
    op.execute(_read("custody.sql"))
    op.execute(_read("transfers.sql"))
    op.execute(
        _read("prior_dispatch.sql").replace(
            "        ELSE RAISE EXCEPTION",
            "        WHEN 'platform_move' THEN\n"
            "            PERFORM public.coinpup_validate_platform_posting("
            "journal_identifier, common_result);\n"
            "        ELSE RAISE EXCEPTION",
        )
    )


def downgrade():
    op.execute("SELECT pg_advisory_xact_lock(18945999704708432)")
    op.execute("""
        LOCK TABLE public.assets, public.accounts, public.platform_transfers
            IN ACCESS EXCLUSIVE MODE;
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM public.assets WHERE kind = 'custodial')
                OR EXISTS (SELECT 1 FROM public.accounts WHERE custody_mode IS NOT NULL)
                OR EXISTS (SELECT 1 FROM public.platform_transfers)
                OR EXISTS (SELECT 1 FROM public.change_log WHERE entity_type = 'platform_transfers')
            THEN
                RAISE EXCEPTION USING ERRCODE = '23514',
                    MESSAGE = 'Platform custody history prevents downgrade',
                    CONSTRAINT = 'ck_platform_transfers_downgrade';
            END IF;
        END; $$;
    """)
    op.execute(_read("prior_dispatch.sql"))
    op.execute(_read("downgrade_triggers.sql"))
    op.drop_table("platform_transfers")
    op.execute(_read("downgrade_checks.sql"))
    op.drop_column("accounts", "custody_mode")
    op.alter_column("assets", "kind", existing_type=sa.String(9), type_=sa.String(8))
