"""Prepared notification configuration model; requires its own reviewed migration."""

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

CLOCK_FIELDS = (
    "planned_for_date",
    "planned_at",
    "planned_timezone",
    "planned_minute",
    "planned_lead_days",
    "resolved_minute",
    "planned_tzdata_version",
)


class ReminderDeliveryConfig(Base):
    __tablename__ = "reminder_delivery_configs"
    __table_args__ = (
        UniqueConstraint("id", "ledger_id", name="uq_reminder_delivery_configs_id_ledger"),
        UniqueConstraint("event_id", name="uq_reminder_delivery_config_event"),
        ForeignKeyConstraint(
            ["event_id", "ledger_id"],
            ["reminder_events.id", "reminder_events.ledger_id"],
            name="fk_reminder_delivery_config_event",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["ledger_id", "last_actor_id"],
            ["ledgers.id", "ledgers.owner_id"],
            name="fk_reminder_delivery_config_actor",
            ondelete="RESTRICT",
        ),
        CheckConstraint("version > 0", name="ck_reminder_delivery_config_version"),
        CheckConstraint("lead_days BETWEEN 0 AND 3660", name="ck_reminder_delivery_config_lead"),
        CheckConstraint("minute BETWEEN 0 AND 1439", name="ck_reminder_delivery_config_minute"),
        CheckConstraint("locale IN ('zh','en')", name="ck_reminder_delivery_config_locale"),
        CheckConstraint(
            "NOT enabled OR inbox_enabled OR email_enabled",
            name="ck_reminder_delivery_config_channel",
        ),
        CheckConstraint(
            "(NOT email_enabled OR email_recipient IS NOT NULL) AND "
            "(email_recipient IS NULL OR (btrim(email_recipient) <> '' AND "
            "email_recipient !~ '[[:cntrl:]]'))",
            name="ck_reminder_delivery_config_recipient",
        ),
        CheckConstraint(
            "timezone ~ '^[A-Za-z0-9_+-]+(/[A-Za-z0-9_+-]+)*$' AND "
            "timezone NOT IN ('localtime','posixrules') AND "
            "timezone NOT LIKE 'posix/%' AND timezone NOT LIKE 'right/%'",
            name="ck_reminder_delivery_config_timezone",
        ),
        CheckConstraint(
            "("
            + " AND ".join(f"{name} IS NULL" for name in CLOCK_FIELDS)
            + ") OR ("
            + " AND ".join(f"{name} IS NOT NULL" for name in CLOCK_FIELDS)
            + ")",
            name="ck_reminder_delivery_config_plan_complete",
        ),
        CheckConstraint(
            "planned_for_date IS NULL OR (planned_for_date BETWEEN DATE '0001-01-01' "
            "AND DATE '9999-12-31' AND isfinite(planned_at) AND "
            "EXTRACT(YEAR FROM planned_at AT TIME ZONE 'UTC') BETWEEN 1 AND 9999 AND "
            "planned_lead_days BETWEEN 0 AND 3660 AND planned_minute BETWEEN 0 AND 1439 AND "
            "resolved_minute BETWEEN planned_minute AND 1439 AND "
            "btrim(planned_timezone) <> '' AND btrim(planned_tzdata_version) <> '')",
            name="ck_reminder_delivery_config_plan_range",
        ),
        CheckConstraint(
            "blocked_reason IS NOT NULL OR (enabled AND planned_at IS NOT NULL AND "
            "timezone = planned_timezone AND minute = planned_minute AND "
            "lead_days = planned_lead_days)",
            name="ck_reminder_delivery_config_ready",
        ),
        CheckConstraint(
            "blocked_reason IS NULL OR blocked_reason IN ('disabled','unplanned','missing_date',"
            "'completed','archived_event','archived_entity','timezone_unavailable',"
            "'date_out_of_range','local_date_unavailable')",
            name="ck_reminder_delivery_config_blocked",
        ),
        CheckConstraint(
            "last_action IN ('create','configure','plan')",
            name="ck_reminder_delivery_config_action",
        ),
    )
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    ledger_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, index=True)
    event_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    lead_days: Mapped[int] = mapped_column(Integer, nullable=False)
    timezone: Mapped[str] = mapped_column(String(64), nullable=False)
    minute: Mapped[int] = mapped_column(Integer, nullable=False)
    locale: Mapped[str] = mapped_column(String(2), nullable=False)
    inbox_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False)
    email_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False)
    email_recipient: Mapped[str | None] = mapped_column(String(320))
    planned_for_date: Mapped[date | None] = mapped_column(Date)
    planned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    planned_timezone: Mapped[str | None] = mapped_column(String(64))
    planned_minute: Mapped[int | None] = mapped_column(Integer)
    planned_lead_days: Mapped[int | None] = mapped_column(Integer)
    resolved_minute: Mapped[int | None] = mapped_column(Integer)
    planned_tzdata_version: Mapped[str | None] = mapped_column(String(32))
    blocked_reason: Mapped[str | None] = mapped_column(String(32))
    last_actor_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    last_action: Mapped[str] = mapped_column(String(16), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class ReminderDeliveryConfigRevision(Base):
    __tablename__ = "reminder_delivery_config_revisions"
    __table_args__ = (
        UniqueConstraint("id", "ledger_id", name="uq_reminder_delivery_config_revisions_id_ledger"),
        UniqueConstraint(
            "config_id", "version", name="uq_reminder_delivery_config_revision_version"
        ),
        ForeignKeyConstraint(
            ["config_id", "ledger_id"],
            ["reminder_delivery_configs.id", "reminder_delivery_configs.ledger_id"],
            name="fk_reminder_delivery_config_revision",
            ondelete="RESTRICT",
        ),
        CheckConstraint("version > 0", name="ck_reminder_delivery_config_revision_version"),
        CheckConstraint(
            "jsonb_typeof(snapshot) = 'object'",
            name="ck_reminder_delivery_config_revision_snapshot",
        ),
    )
    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, server_default=text("gen_random_uuid()")
    )
    ledger_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, index=True)
    config_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    snapshot: Mapped[dict] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
