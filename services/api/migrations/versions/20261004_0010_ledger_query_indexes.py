"""Cover original-asset balance totals and ledger-scoped journal dates."""

import sqlalchemy as sa
from alembic import op

revision = "20261004_0010"
down_revision = "20261004_0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "ix_journal_lines_account_balance",
        "journal_lines",
        ["ledger_id", "account_id", "asset_id"],
        postgresql_include=["amount"],
        postgresql_where=sa.text("role = 'account'"),
    )
    op.create_index(
        "ix_journals_ledger_transaction_date", "journals", ["ledger_id", "transaction_date"]
    )
    op.create_index(
        "ix_journals_ledger_recognition_date", "journals", ["ledger_id", "recognition_date"]
    )


def downgrade() -> None:
    op.drop_index("ix_journals_ledger_recognition_date", table_name="journals")
    op.drop_index("ix_journals_ledger_transaction_date", table_name="journals")
    op.drop_index("ix_journal_lines_account_balance", table_name="journal_lines")
