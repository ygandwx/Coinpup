"""One frozen in-app notification and a permanent first-read timestamp."""

from datetime import date, datetime
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from coinpup_api.models import Base


class ReminderInboxDelivery(Base):
    __tablename__ = "reminder_inbox_deliveries"
    __table_args__ = (
        UniqueConstraint("event_id", "channel", "planned_at", name="uq_reminder_inbox_schedule"),
        ForeignKeyConstraint(
            ["event_id", "ledger_id"],
            ["reminder_events.id", "reminder_events.ledger_id"],
            name="fk_reminder_inbox_event",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["event_revision_id", "ledger_id"],
            ["reminder_event_revisions.id", "reminder_event_revisions.ledger_id"],
            name="fk_reminder_inbox_event_revision",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["config_revision_id", "ledger_id"],
            ["reminder_delivery_revisions.id", "reminder_delivery_revisions.ledger_id"],
            name="fk_reminder_inbox_config_revision",
            ondelete="RESTRICT",
        ),
        CheckConstraint("channel = 'inbox'", name="ck_reminder_inbox_channel"),
        CheckConstraint(
            "event_kind IN ('annual','tax','certificate')", name="ck_reminder_inbox_kind"
        ),
        CheckConstraint("locale IN ('en','zh')", name="ck_reminder_inbox_locale"),
        CheckConstraint(
            "date_basis IN ('manual','calculated')", name="ck_reminder_inbox_date_basis"
        ),
        CheckConstraint("planned_at <= delivered_at", name="ck_reminder_inbox_delivered"),
        CheckConstraint(
            "(version = 1 AND read_at IS NULL) OR "
            "(version = 2 AND read_at IS NOT NULL AND read_at >= delivered_at)",
            name="ck_reminder_inbox_read_version",
        ),
        Index("ix_reminder_inbox_ledger_schedule", "ledger_id", "planned_at", "id"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    ledger_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    event_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    event_revision_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    config_revision_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    channel: Mapped[str] = mapped_column(String(8), server_default=text("'inbox'"), nullable=False)
    version: Mapped[int] = mapped_column(Integer, server_default=text("1"), nullable=False)
    planned_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    delivered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    title: Mapped[str] = mapped_column(String(160), nullable=False)
    event_kind: Mapped[str] = mapped_column(String(12), nullable=False)
    due_date: Mapped[date] = mapped_column(Date, nullable=False)
    date_basis: Mapped[str] = mapped_column(String(10), nullable=False)
    locale: Mapped[str] = mapped_column(String(2), nullable=False)
