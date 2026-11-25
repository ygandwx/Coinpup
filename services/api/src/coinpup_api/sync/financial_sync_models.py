"""Permanent, owner-scoped device intent receipts and explicit keep-current decisions."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Integer,
    String,
    Text,
    Uuid,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from coinpup_api.models import Base


class FinancialPushRecord(Base):
    __tablename__ = "sync_financial_pushes"
    __table_args__ = (
        ForeignKeyConstraint(
            ["owner_id", "device_id", "registration_version"],
            [
                "sync_device_revisions.owner_id",
                "sync_device_revisions.device_id",
                "sync_device_revisions.version",
            ],
            name="fk_sync_financial_push_device",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["ledger_id", "owner_id"],
            ["ledgers.id", "ledgers.owner_id"],
            name="fk_sync_financial_push_ledger",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["operation_id", "ledger_id"],
            ["financial_operations.id", "financial_operations.ledger_id"],
            name="fk_sync_financial_push_operation",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["ledger_id", "receipt_key"],
            ["command_receipts.ledger_id", "command_receipts.key"],
            name="fk_sync_financial_push_receipt",
            ondelete="RESTRICT",
        ),
        CheckConstraint("registration_version = 1", name="ck_sync_financial_push_registration"),
        CheckConstraint("command_key ~ '^[!-~]{1,128}$'", name="ck_sync_financial_push_key"),
        CheckConstraint(
            "request_hash ~ '^[0-9a-f]{64}$' AND ledger_digest ~ '^[0-9a-f]{64}$'",
            name="ck_sync_financial_push_hashes",
        ),
        CheckConstraint(
            "octet_length(request_body) BETWEEN 1 AND 131072",
            name="ck_sync_financial_push_body",
        ),
        CheckConstraint(
            "jsonb_typeof(response) = 'object' AND "
            "(response->>'outcome' IS NOT DISTINCT FROM outcome)",
            name="ck_sync_financial_push_response",
        ),
        CheckConstraint(
            "(outcome = 'applied' AND receipt_key IS NOT NULL AND receipt_key = command_key "
            "AND basis_version IS NULL AND competing_version IS NULL) OR "
            "(outcome = 'conflict' AND receipt_key IS NULL AND basis_version IS NOT NULL "
            "AND competing_version IS NOT NULL AND basis_version > 0 "
            "AND competing_version > basis_version)",
            name="ck_sync_financial_push_outcome",
        ),
    )

    owner_id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    device_id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    intent_id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    registration_version: Mapped[int] = mapped_column(Integer, server_default="1")
    ledger_id: Mapped[UUID] = mapped_column(Uuid)
    operation_id: Mapped[UUID] = mapped_column(Uuid)
    command_key: Mapped[str] = mapped_column(String(128))
    request_hash: Mapped[str] = mapped_column(String(64))
    ledger_digest: Mapped[str] = mapped_column(String(64))
    request_body: Mapped[str] = mapped_column(Text)
    outcome: Mapped[str] = mapped_column(String(8))
    receipt_key: Mapped[str | None] = mapped_column(String(128))
    basis_version: Mapped[int | None] = mapped_column(Integer)
    competing_version: Mapped[int | None] = mapped_column(Integer)
    response: Mapped[dict] = mapped_column(JSONB)
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class FinancialResolutionRecord(Base):
    __tablename__ = "sync_financial_conflict_resolutions"
    __table_args__ = (
        ForeignKeyConstraint(
            ["owner_id", "device_id", "intent_id"],
            [
                "sync_financial_pushes.owner_id",
                "sync_financial_pushes.device_id",
                "sync_financial_pushes.intent_id",
            ],
            name="fk_sync_financial_resolution_push",
            ondelete="RESTRICT",
        ),
        CheckConstraint("accepted_version > 0", name="ck_sync_financial_resolution_version"),
    )

    owner_id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    device_id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    intent_id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    accepted_version: Mapped[int] = mapped_column(Integer)
    resolved_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
