"""Optional per-ledger closing state and permanent reasoned command history."""

import uuid
from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    ForeignKeyConstraint,
    Integer,
    String,
    UniqueConstraint,
    Uuid,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from coinpup_api.models import Base


def scope(table):
    return (
        UniqueConstraint("id", "ledger_id", name=f"uq_{table}_id_ledger"),
        ForeignKeyConstraint(
            ["ledger_id"], ["ledgers.id"], name=f"fk_{table}_ledger", ondelete="RESTRICT"
        ),
    )


class PeriodIdentity:
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    ledger_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, index=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class LedgerPeriod(PeriodIdentity, Base):
    __tablename__ = "ledger_periods"
    __table_args__ = (
        *scope(__tablename__),
        UniqueConstraint("ledger_id", name="uq_ledger_periods_ledger"),
        CheckConstraint("version >= 2", name="ck_ledger_periods_version"),
    )
    closed_through: Mapped[date | None] = mapped_column(Date)


class PeriodAudit(PeriodIdentity, Base):
    __tablename__ = "ledger_period_audits"
    __table_args__ = (
        *scope(__tablename__),
        UniqueConstraint("ledger_id", "version", name="uq_ledger_period_audits_version"),
        ForeignKeyConstraint(
            ["period_id", "ledger_id"],
            ["ledger_periods.id", "ledger_periods.ledger_id"],
            name="fk_period_audit_state",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["ledger_id", "actor_id"],
            ["ledgers.id", "ledgers.owner_id"],
            name="fk_period_audit_actor",
            ondelete="RESTRICT",
        ),
        CheckConstraint("version >= 2", name="ck_ledger_period_audits_version"),
        CheckConstraint("btrim(reason) <> ''", name="ck_period_audit_reason"),
        CheckConstraint(
            "(action = 'close' AND closed_through IS NOT NULL AND "
            "(previous_closed_through IS NULL OR closed_through >= previous_closed_through)) "
            "OR (action = 'reopen' AND previous_closed_through IS NOT NULL AND "
            "(closed_through IS NULL OR closed_through < previous_closed_through))",
            name="ck_period_audit_transition",
        ),
    )
    period_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    actor_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    action: Mapped[str] = mapped_column(String(6), nullable=False)
    reason: Mapped[str] = mapped_column(String(1000), nullable=False)
    previous_closed_through: Mapped[date | None] = mapped_column(Date)
    closed_through: Mapped[date | None] = mapped_column(Date)


class PeriodReceipt(PeriodIdentity, Base):
    __tablename__ = "ledger_period_receipts"
    __table_args__ = (
        *scope(__tablename__),
        UniqueConstraint("ledger_id", "idempotency_key", name="uq_period_receipt_key"),
        UniqueConstraint("audit_id", name="uq_period_receipt_audit"),
        ForeignKeyConstraint(
            ["audit_id", "ledger_id"],
            ["ledger_period_audits.id", "ledger_period_audits.ledger_id"],
            name="fk_period_receipt_audit",
            ondelete="RESTRICT",
        ),
        CheckConstraint("version = 1 AND hash_version = 2", name="ck_period_receipt_version"),
        CheckConstraint("request_hash ~ '^[0-9a-f]{64}$'", name="ck_period_receipt_hash"),
        CheckConstraint("idempotency_key ~ '^[!-~]{1,128}$'", name="ck_period_receipt_key"),
        CheckConstraint("jsonb_typeof(response) = 'object'", name="ck_period_receipt_response"),
    )
    audit_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    hash_version: Mapped[int] = mapped_column(Integer, nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    response: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
