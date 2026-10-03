"""Single administrator, revocable sessions and a persistent login guard."""

import sqlalchemy as sa
from alembic import op

revision = "20261003_0002"
down_revision = "20261003_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "administrators",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("singleton", sa.Integer(), nullable=False),
        sa.Column("username", sa.String(64), nullable=False),
        sa.Column("password_hash", sa.Text(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint("singleton = 1", name="ck_administrator_singleton"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("singleton"),
        sa.UniqueConstraint("username"),
    )
    op.create_table(
        "auth_sessions",
        sa.Column("token_hash", sa.String(64), nullable=False),
        sa.Column("administrator_id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["administrator_id"], ["administrators.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("token_hash"),
    )
    op.create_index("ix_auth_sessions_administrator_id", "auth_sessions", ["administrator_id"])
    op.create_index("ix_auth_sessions_expires_at", "auth_sessions", ["expires_at"])
    op.create_table(
        "auth_login_guard",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("failure_count", sa.Integer(), nullable=False),
        sa.Column("window_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("locked_until", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("id = 1", name="ck_login_guard_singleton"),
        sa.CheckConstraint("failure_count >= 0", name="ck_login_guard_failures"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.bulk_insert(
        sa.table(
            "auth_login_guard",
            sa.column("id", sa.Integer()),
            sa.column("failure_count", sa.Integer()),
        ),
        [{"id": 1, "failure_count": 0}],
    )


def downgrade() -> None:
    op.drop_table("auth_sessions")
    op.drop_table("administrators")
    op.drop_table("auth_login_guard")
