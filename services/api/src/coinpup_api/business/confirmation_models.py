"""Immutable business confirmation groups and same-ledger posting membership."""

import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Index,
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


class BusinessConfirmation(Base):
    __tablename__ = "business_confirmations"
    __table_args__ = (
        Index("ix_business_confirmations_ledger_created", "ledger_id", "created_at", "id"),
        UniqueConstraint("id", "document_id", "ledger_id", name="uq_business_confirmation_scope"),
        UniqueConstraint("document_id", name="uq_business_confirmation_document"),
        UniqueConstraint("ledger_id", "idempotency_key", name="uq_business_confirmation_key"),
        ForeignKeyConstraint(
            ["document_id", "ledger_id"],
            ["business_documents.id", "business_documents.ledger_id"],
            name="fk_business_confirmation_document",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["ledger_id", "created_by"],
            ["ledgers.id", "ledgers.owner_id"],
            name="fk_business_confirmation_owner",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["ledger_id", "idempotency_key"],
            ["command_receipts.ledger_id", "command_receipts.key"],
            name="fk_business_confirmation_receipt",
            deferrable=True,
            initially="DEFERRED",
        ),
        CheckConstraint(
            "version = 1 AND source_version > 0", name="ck_business_confirmation_version"
        ),
        CheckConstraint(
            "jsonb_typeof(snapshot) = 'object'", name="ck_business_confirmation_snapshot"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    ledger_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    document_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    created_by: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    source_version: Mapped[int] = mapped_column(Integer, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    snapshot: Mapped[dict] = mapped_column(JSONB(none_as_null=True), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class BusinessPosting(Base):
    __tablename__ = "business_postings"
    __table_args__ = (
        Index("ix_business_postings_confirmation", "confirmation_id", "line_id"),
        UniqueConstraint("line_id", name="uq_business_posting_line"),
        UniqueConstraint("operation_id", name="uq_business_posting_operation"),
        UniqueConstraint("journal_id", name="uq_business_posting_journal"),
        ForeignKeyConstraint(
            ["confirmation_id", "document_id", "ledger_id"],
            [
                "business_confirmations.id",
                "business_confirmations.document_id",
                "business_confirmations.ledger_id",
            ],
            name="fk_business_posting_confirmation",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["line_id", "document_id", "ledger_id"],
            [
                "business_document_lines.id",
                "business_document_lines.document_id",
                "business_document_lines.ledger_id",
            ],
            name="fk_business_posting_line",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["journal_id", "operation_id", "ledger_id"],
            ["journals.id", "journals.operation_id", "journals.ledger_id"],
            name="fk_business_posting_journal",
            deferrable=True,
            initially="DEFERRED",
        ),
        CheckConstraint("version = 1", name="ck_business_posting_version"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, server_default=text("gen_random_uuid()")
    )
    ledger_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    confirmation_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    document_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    line_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    operation_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    journal_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
