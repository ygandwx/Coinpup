"""Append-only notifications; financial facts remain in the existing ledger tables."""

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Identity,
    Index,
    Integer,
    String,
    Text,
    Uuid,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from coinpup_api.models import Base


class ChangeLog(Base):
    __tablename__ = "change_log"
    __table_args__ = (
        ForeignKeyConstraint(
            ["ledger_id", "owner_id"],
            ["ledgers.id", "ledgers.owner_id"],
            name="fk_change_log_ledger_owner",
            ondelete="RESTRICT",
        ),
        CheckConstraint("seq > 0", name="ck_change_log_seq"),
        CheckConstraint("entity_version > 0", name="ck_change_log_entity_version"),
        CheckConstraint("btrim(entity_id) <> ''", name="ck_change_log_entity_id"),
        CheckConstraint(
            "entity_type IN ('entities', 'ledgers', 'accounts', 'account_assets', "
            "'categories', 'assets', 'financial_operations', 'stored_files', "
            "'operation_file_links', 'ocr_jobs', 'ocr_drafts', 'ocr_confirmations')",
            name="ck_change_log_entity_type",
        ),
        CheckConstraint(
            "change_kind IN ('upsert', 'archive', 'restore', 'cancel')",
            name="ck_change_log_change_kind",
        ),
        Index("ix_change_log_owner_seq", "owner_id", "seq"),
    )

    seq: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    owner_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("administrators.id", name="fk_change_log_owner", ondelete="RESTRICT"),
        nullable=False,
    )
    ledger_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    entity_type: Mapped[str] = mapped_column(String(32), nullable=False)
    entity_id: Mapped[str] = mapped_column(Text, nullable=False)
    entity_version: Mapped[int] = mapped_column(Integer, nullable=False)
    change_kind: Mapped[str] = mapped_column(String(8), nullable=False)
    changed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.clock_timestamp()
    )
