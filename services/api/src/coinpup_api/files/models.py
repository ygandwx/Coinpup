"""Immutable stored content and mutable document metadata with ledger ownership."""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    String,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from coinpup_api.ledger.models import Versioned
from coinpup_api.models import Base


class StoredFile(Versioned, Base):
    __tablename__ = "stored_files"
    __table_args__ = (
        UniqueConstraint("id", "ledger_id", name="uq_stored_files_id_ledger"),
        UniqueConstraint("blob_key", name="uq_stored_files_blob_key"),
        UniqueConstraint("ledger_id", "sha256", "byte_size", name="uq_stored_files_ledger_content"),
        ForeignKeyConstraint(
            ["ledger_id", "created_by"],
            ["ledgers.id", "ledgers.owner_id"],
            name="fk_stored_files_ledger_owner",
            ondelete="RESTRICT",
        ),
        CheckConstraint("blob_key ~ '^[0-9a-f]{32}$'", name="ck_stored_files_blob_key"),
        CheckConstraint("sha256 ~ '^[0-9a-f]{64}$'", name="ck_stored_files_sha256"),
        CheckConstraint("byte_size > 0", name="ck_stored_files_byte_size"),
        CheckConstraint(
            "detected_media_type IN ('application/pdf', 'image/jpeg', 'image/png', 'image/webp')",
            name="ck_stored_files_media_type",
        ),
        CheckConstraint(
            "original_filename ~ '[^[:space:]]' AND original_filename !~ '[[:cntrl:]]'",
            name="ck_stored_files_filename",
        ),
        CheckConstraint("title ~ '[^[:space:]]'", name="ck_stored_files_title"),
        CheckConstraint("version > 0", name="ck_stored_files_version"),
        CheckConstraint("updated_at >= created_at", name="ck_stored_files_timestamps"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    ledger_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, index=True)
    created_by: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    blob_key: Mapped[str] = mapped_column(String(32), nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    byte_size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    detected_media_type: Mapped[str] = mapped_column(String(64), nullable=False)
    original_filename: Mapped[str] = mapped_column(String(255), nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    archived: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )


class FileUpload(Base):
    __tablename__ = "file_uploads"
    __table_args__ = (
        UniqueConstraint("id", "ledger_id", name="uq_file_uploads_id_ledger"),
        ForeignKeyConstraint(
            ["ledger_id", "created_by"],
            ["ledgers.id", "ledgers.owner_id"],
            name="fk_file_uploads_ledger_owner",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["operation_id", "ledger_id"],
            ["financial_operations.id", "financial_operations.ledger_id"],
            name="fk_file_uploads_operation_ledger",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["file_id", "ledger_id"],
            ["stored_files.id", "stored_files.ledger_id"],
            name="fk_file_uploads_file_ledger",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "original_filename ~ '[^[:space:]]' AND original_filename !~ '[[:cntrl:]]'",
            name="ck_file_uploads_filename",
        ),
        CheckConstraint("declared_size > 0", name="ck_file_uploads_declared_size"),
        CheckConstraint("manifest_hash ~ '^[0-9a-f]{64}$'", name="ck_file_uploads_manifest_hash"),
        CheckConstraint(
            "(state = 'pending' AND file_id IS NULL AND response IS NULL "
            "AND completed_at IS NULL) OR "
            "(state = 'ready' AND file_id IS NOT NULL AND response IS NOT NULL "
            "AND completed_at IS NOT NULL)",
            name="ck_file_uploads_completion",
        ),
        CheckConstraint(
            "response IS NULL OR jsonb_typeof(response) = 'object'",
            name="ck_file_uploads_response",
        ),
        CheckConstraint(
            "completed_at IS NULL OR completed_at >= created_at",
            name="ck_file_uploads_timestamps",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    ledger_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, index=True)
    created_by: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    original_filename: Mapped[str] = mapped_column(String(255), nullable=False)
    declared_size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    operation_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True, index=True)
    manifest_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    state: Mapped[str] = mapped_column(
        String(8), nullable=False, default="pending", server_default="pending"
    )
    file_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True, index=True)
    response: Mapped[dict[str, Any] | None] = mapped_column(JSONB(none_as_null=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class OperationFileLink(Versioned, Base):
    __tablename__ = "operation_file_links"
    __table_args__ = (
        UniqueConstraint("id", "ledger_id", name="uq_operation_file_links_id_ledger"),
        UniqueConstraint("file_id", "operation_id", name="uq_operation_file_links_pair"),
        ForeignKeyConstraint(
            ["ledger_id", "created_by"],
            ["ledgers.id", "ledgers.owner_id"],
            name="fk_operation_file_links_ledger_owner",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["file_id", "ledger_id"],
            ["stored_files.id", "stored_files.ledger_id"],
            name="fk_operation_file_links_file_ledger",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["operation_id", "ledger_id"],
            ["financial_operations.id", "financial_operations.ledger_id"],
            name="fk_operation_file_links_operation_ledger",
            ondelete="RESTRICT",
        ),
        CheckConstraint("version > 0", name="ck_operation_file_links_version"),
        CheckConstraint("updated_at >= created_at", name="ck_operation_file_links_timestamps"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    ledger_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, index=True)
    file_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, index=True)
    operation_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, index=True)
    created_by: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    archived: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
