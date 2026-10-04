"""Keep legacy receipt hashes identifiable while new commands use raw-JSON v2."""

import sqlalchemy as sa
from alembic import op

revision = "20261004_0009"
down_revision = "20261003_0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "command_receipts",
        sa.Column("hash_version", sa.SmallInteger(), server_default="1", nullable=False),
    )
    op.create_check_constraint(
        "ck_command_receipts_hash_version", "command_receipts", "hash_version IN (1, 2)"
    )


def downgrade() -> None:
    op.execute("""
        LOCK TABLE command_receipts IN ACCESS EXCLUSIVE MODE;
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM command_receipts WHERE hash_version <> 1) THEN
                RAISE EXCEPTION USING ERRCODE = '23514',
                    MESSAGE = 'Versioned command receipts prevent this downgrade',
                    CONSTRAINT = 'ck_command_receipts_hash_version_downgrade';
            END IF;
        END $$
    """)
    op.drop_constraint("ck_command_receipts_hash_version", "command_receipts", type_="check")
    op.drop_column("command_receipts", "hash_version")
