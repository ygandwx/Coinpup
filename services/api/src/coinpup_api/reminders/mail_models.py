"""Permanent email identities and append-only worker and user evidence."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Integer,
    String,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from coinpup_api.models import Base


class ReminderEmailDelivery(Base):
    __tablename__ = "reminder_email_deliveries"
    __table_args__ = (
        UniqueConstraint("id", "ledger_id", name="uq_reminder_email_delivery_ledger"),
        UniqueConstraint("event_id", "channel", "planned_at", name="uq_reminder_email_schedule"),
        ForeignKeyConstraint(
            ["event_id", "ledger_id"],
            ["reminder_events.id", "reminder_events.ledger_id"],
            name="fk_reminder_email_event",
            ondelete="RESTRICT",
        ),
        CheckConstraint("channel = 'email'", name="ck_reminder_email_channel"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    ledger_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    event_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    channel: Mapped[str] = mapped_column(String(8), nullable=False)
    planned_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ReminderEmailEvidence(Base):
    __tablename__ = "reminder_email_evidence"
    __table_args__ = (
        UniqueConstraint("delivery_id", "version", name="uq_reminder_email_evidence_version"),
        ForeignKeyConstraint(
            ["delivery_id", "ledger_id"],
            ["reminder_email_deliveries.id", "reminder_email_deliveries.ledger_id"],
            name="fk_reminder_email_evidence_delivery",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["event_revision_id", "ledger_id"],
            ["reminder_event_revisions.id", "reminder_event_revisions.ledger_id"],
            name="fk_reminder_email_evidence_event",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["config_revision_id", "ledger_id"],
            ["reminder_delivery_revisions.id", "reminder_delivery_revisions.ledger_id"],
            name="fk_reminder_email_evidence_config",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "version > 0 AND attempt_no > 0 AND retries_used BETWEEN 0 AND 5",
            name="ck_reminder_email_evidence_sequence",
        ),
        CheckConstraint(
            "state IN ('sending','retry_wait','failed','unknown','accepted','cancelled',"
            "'verified_received','stopped')",
            name="ck_reminder_email_evidence_state",
        ),
        CheckConstraint(
            "(state = 'sending' AND lease_until > recorded_at AND envelope IS NOT NULL "
            "AND jsonb_typeof(envelope) = 'object' AND event_revision_id IS NOT NULL "
            "AND config_revision_id IS NOT NULL AND lease_until IS NOT NULL) OR "
            "(state <> 'sending' AND lease_until IS NULL AND envelope IS NULL AND "
            "event_revision_id IS NULL AND config_revision_id IS NULL)",
            name="ck_reminder_email_evidence_claim",
        ),
        CheckConstraint(
            "(state = 'retry_wait' AND retry_at IS NOT NULL AND retry_at >= recorded_at) OR "
            "(state <> 'retry_wait' AND retry_at IS NULL)",
            name="ck_reminder_email_evidence_retry",
        ),
        CheckConstraint(
            "smtp_code IS NULL OR (state = 'accepted' AND smtp_code BETWEEN 200 AND 299) OR "
            "(state = 'retry_wait' AND smtp_code BETWEEN 400 AND 499) OR "
            "(state = 'failed' AND smtp_code BETWEEN 400 AND 599)",
            name="ck_reminder_email_evidence_smtp_code",
        ),
        CheckConstraint(
            "(state IN ('verified_received','stopped') AND reason IS NOT NULL "
            "AND btrim(reason) <> '') OR "
            "(state NOT IN ('verified_received','stopped') AND reason IS NULL)",
            name="ck_reminder_email_evidence_reason",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    delivery_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    ledger_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    attempt_no: Mapped[int] = mapped_column(Integer, nullable=False)
    state: Mapped[str] = mapped_column(String(24), nullable=False)
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    retries_used: Mapped[int] = mapped_column(Integer, nullable=False)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    smtp_code: Mapped[int | None] = mapped_column(Integer)
    reason: Mapped[str | None] = mapped_column(String(2000))
    event_revision_id: Mapped[UUID | None] = mapped_column(Uuid)
    config_revision_id: Mapped[UUID | None] = mapped_column(Uuid)
    envelope: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True))
