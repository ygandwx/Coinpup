"""Immutable device registry evidence, scoped to the authenticated owner; grants no access."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Integer,
    String,
    Uuid,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from coinpup_api.models import Base


class DeviceRevision(Base):
    __tablename__ = "sync_device_revisions"
    __table_args__ = (
        CheckConstraint("start_seq >= 0", name="ck_sync_device_start_seq"),
        CheckConstraint(
            "(version = 1 AND state = 'registered' AND previous_version IS NULL) OR "
            "(version = 2 AND state = 'retired' AND previous_version IS NOT NULL "
            "AND previous_version = 1)",
            name="ck_sync_device_transition",
        ),
        CheckConstraint(
            "isfinite(recorded_at) AND EXTRACT(YEAR FROM recorded_at AT TIME ZONE 'UTC') "
            "BETWEEN 1 AND 9999",
            name="ck_sync_device_time",
        ),
        ForeignKeyConstraint(
            ["owner_id", "device_id", "previous_version"],
            [
                "sync_device_revisions.owner_id",
                "sync_device_revisions.device_id",
                "sync_device_revisions.version",
            ],
            name="fk_sync_device_origin",
            ondelete="RESTRICT",
        ),
    )
    owner_id: Mapped[UUID] = mapped_column(
        Uuid,
        ForeignKey("administrators.id", ondelete="RESTRICT"),
        primary_key=True,
    )
    device_id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    version: Mapped[int] = mapped_column(Integer, primary_key=True)
    previous_version: Mapped[int | None] = mapped_column(Integer)
    start_seq: Mapped[int] = mapped_column(BigInteger, server_default=text("0"))
    state: Mapped[str] = mapped_column(String(10), nullable=False)
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
