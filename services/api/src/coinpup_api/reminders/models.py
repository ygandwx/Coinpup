"""Scoped reminder state and immutable database-captured revisions (ADR 0036)."""

import uuid
from datetime import date, datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKeyConstraint,
    Integer,
    String,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from coinpup_api.models import Base


class ReminderEvent(Base):
    __tablename__ = "reminder_events"
    __table_args__ = (
        UniqueConstraint("id", "ledger_id", name="uq_reminder_events_id_ledger"),
        ForeignKeyConstraint(
            ["ledger_id", "last_actor_id"],
            ["ledgers.id", "ledgers.owner_id"],
            name="fk_reminder_event_actor",
            ondelete="RESTRICT",
        ),
        CheckConstraint("version > 0", name="ck_reminder_event_version"),
        CheckConstraint(
            "event_kind IN ('annual', 'tax', 'certificate')", name="ck_reminder_event_kind"
        ),
        CheckConstraint("btrim(title) <> ''", name="ck_reminder_event_title"),
        CheckConstraint(
            "evaluation_status IN ('calculated', 'missing_parameters', "
            "'needs_verification', 'not_applicable')",
            name="ck_reminder_event_status",
        ),
        CheckConstraint(
            "coinpup_valid_reminder_evaluation(evaluation, evaluation_status, calculated_date)",
            name="ck_reminder_event_evaluation",
        ),
        CheckConstraint(
            "(manual_due_date IS NULL AND manual_reason IS NULL) OR "
            "(manual_due_date IS NOT NULL "
            "AND manual_due_date BETWEEN DATE '0001-01-01' AND DATE '9999-12-31' "
            "AND manual_reason IS NOT NULL AND btrim(manual_reason) <> '')",
            name="ck_reminder_event_manual",
        ),
        CheckConstraint(
            "last_action IN ('create', 'edit', 'recalculate', 'set_manual', "
            "'clear_manual', 'complete', 'reopen', 'archive', 'restore')",
            name="ck_reminder_event_action",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    ledger_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, index=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    event_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    title: Mapped[str] = mapped_column(String(160), nullable=False)
    notes: Mapped[str | None] = mapped_column(String(2000))
    evaluation: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True))
    evaluation_status: Mapped[str] = mapped_column(String(24), nullable=False)
    calculated_date: Mapped[date | None] = mapped_column(Date)
    manual_due_date: Mapped[date | None] = mapped_column(Date)
    manual_reason: Mapped[str | None] = mapped_column(String(500))
    completed: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    archived: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    last_actor_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    last_action: Mapped[str] = mapped_column(String(16), nullable=False)
    last_reason: Mapped[str | None] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class ReminderEventRevision(Base):
    __tablename__ = "reminder_event_revisions"
    __table_args__ = (
        UniqueConstraint("id", "ledger_id", name="uq_reminder_event_revisions_id_ledger"),
        UniqueConstraint("event_id", "version", name="uq_reminder_event_revision_version"),
        ForeignKeyConstraint(
            ["event_id", "ledger_id"],
            ["reminder_events.id", "reminder_events.ledger_id"],
            name="fk_reminder_revision_event",
            ondelete="RESTRICT",
        ),
        CheckConstraint("version > 0", name="ck_reminder_revision_version"),
        CheckConstraint("jsonb_typeof(snapshot) = 'object'", name="ck_reminder_revision_snapshot"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, server_default=text("gen_random_uuid()")
    )
    ledger_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, index=True)
    event_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    snapshot: Mapped[dict] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
