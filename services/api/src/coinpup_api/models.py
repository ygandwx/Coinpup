"""Authentication persistence. Business ledger tables are introduced in T02."""

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Integer, String, Text, Uuid, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Administrator(Base):
    __tablename__ = "administrators"
    __table_args__ = (CheckConstraint("singleton = 1", name="ck_administrator_singleton"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    singleton: Mapped[int] = mapped_column(Integer, nullable=False, unique=True, default=1)
    username: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AuthSession(Base):
    __tablename__ = "auth_sessions"

    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    administrator_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("administrators.id", ondelete="CASCADE"), nullable=False, index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class LoginGuard(Base):
    """The seeded singleton row serializes attempts across every API process."""

    __tablename__ = "auth_login_guard"
    __table_args__ = (
        CheckConstraint("id = 1", name="ck_login_guard_singleton"),
        CheckConstraint("failure_count >= 0", name="ck_login_guard_failures"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    failure_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    window_started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
