"""Durable evidence for an owned page of immutable notifications, outside the business stream."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from coinpup_api.models import Base


class PageAcknowledgement(Base):
    __tablename__ = "sync_page_acknowledgements"
    __table_args__ = (
        UniqueConstraint("owner_id", "device_id", "after_seq", name="uq_sync_ack_start"),
        ForeignKeyConstraint(
            ["owner_id", "device_id", "registry_version"],
            [
                "sync_device_revisions.owner_id",
                "sync_device_revisions.device_id",
                "sync_device_revisions.version",
            ],
            name="fk_sync_ack_registration",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["owner_id", "device_id", "prior_tail"],
            [
                "sync_page_acknowledgements.owner_id",
                "sync_page_acknowledgements.device_id",
                "sync_page_acknowledgements.last_seq",
            ],
            name="fk_sync_ack_prior",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "registry_version=1 AND after_seq>=0 AND last_seq>after_seq "
            "AND through_seq>=last_seq AND page_limit BETWEEN 1 AND 200 "
            "AND row_count BETWEEN 1 AND page_limit "
            "AND (prior_tail IS NULL OR after_seq=prior_tail)",
            name="ck_sync_ack_window",
        ),
        CheckConstraint(
            "isfinite(recorded_at) AND EXTRACT(YEAR FROM recorded_at AT TIME ZONE 'UTC') "
            "BETWEEN 1 AND 9999",
            name="ck_sync_ack_time",
        ),
        Index(
            "uq_sync_ack_single_start",
            "owner_id",
            "device_id",
            unique=True,
            postgresql_where=text("prior_tail IS NULL"),
        ),
    )
    owner_id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    device_id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    last_seq: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    registry_version: Mapped[int] = mapped_column(Integer, server_default=text("1"))
    after_seq: Mapped[int] = mapped_column(BigInteger)
    through_seq: Mapped[int] = mapped_column(BigInteger)
    page_limit: Mapped[int] = mapped_column(Integer)
    row_count: Mapped[int] = mapped_column(Integer)
    prior_tail: Mapped[int | None] = mapped_column(BigInteger)
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
