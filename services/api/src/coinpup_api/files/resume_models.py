"""Frozen upload manifest and ordered, privately staged bytes with durable part receipts."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Integer,
    LargeBinary,
    String,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from coinpup_api.models import Base

CHUNK_BYTES = 1024 * 1024


class UploadResume(Base):
    __tablename__ = "file_upload_resumes"
    __table_args__ = (
        UniqueConstraint("upload_id", "ledger_id", name="uq_file_upload_resume_ledger"),
        ForeignKeyConstraint(
            ["upload_id", "ledger_id"],
            ["file_uploads.id", "file_uploads.ledger_id"],
            name="fk_file_upload_resume_reservation",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["ledger_id", "owner_id"],
            ["ledgers.id", "ledgers.owner_id"],
            name="fk_file_upload_resume_owner",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["owner_id", "device_id", "registry_version"],
            [
                "sync_device_revisions.owner_id",
                "sync_device_revisions.device_id",
                "sync_device_revisions.version",
            ],
            name="fk_file_upload_resume_device",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "registry_version = 1 AND sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_file_upload_resume_manifest",
        ),
    )

    upload_id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    ledger_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    owner_id: Mapped[UUID] = mapped_column(Uuid, nullable=False, index=True)
    device_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    registry_version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.clock_timestamp()
    )
    abandoned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class UploadChunk(Base):
    __tablename__ = "file_upload_chunks"
    __table_args__ = (
        ForeignKeyConstraint(
            ["upload_id", "ledger_id"],
            ["file_upload_resumes.upload_id", "file_upload_resumes.ledger_id"],
            name="fk_file_upload_chunk_resume",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "byte_offset >= 0 AND mod(byte_offset, 1048576) = 0 "
            "AND byte_size BETWEEN 1 AND 1048576 AND sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_file_upload_chunk_boundary",
        ),
        CheckConstraint(
            "payload IS NULL OR (octet_length(payload) = byte_size "
            "AND encode(sha256(payload), 'hex') = sha256)",
            name="ck_file_upload_chunk_content",
        ),
    )

    upload_id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    byte_offset: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    ledger_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    byte_size: Mapped[int] = mapped_column(Integer, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[bytes | None] = mapped_column(LargeBinary)
