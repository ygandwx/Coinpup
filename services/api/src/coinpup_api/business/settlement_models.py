"""Immutable actual payments and their explicitly dated source-line applications."""

from datetime import date, datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from coinpup_api.models import Base


def _identity(name):
    return (
        UniqueConstraint("id", "ledger_id", name=f"uq_{name}_id_ledger"),
        ForeignKeyConstraint(
            ["ledger_id"], ["ledgers.id"], name=f"fk_{name}_ledger", ondelete="RESTRICT"
        ),
        CheckConstraint("version = 1", name=f"ck_{name}_version"),
    )


def _journal(name):
    return (
        UniqueConstraint("operation_id", name=f"uq_{name}_operation"),
        UniqueConstraint("journal_id", name=f"uq_{name}_journal"),
        ForeignKeyConstraint(
            ["journal_id", "operation_id", "ledger_id"],
            ["journals.id", "journals.operation_id", "journals.ledger_id"],
            name=f"fk_{name}_journal",
            deferrable=True,
            initially="DEFERRED",
        ),
    )


def _receipt(name):
    return (
        UniqueConstraint("ledger_id", "idempotency_key", name=f"uq_{name}_key"),
        ForeignKeyConstraint(
            ["ledger_id", "created_by"],
            ["ledgers.id", "ledgers.owner_id"],
            name=f"fk_{name}_owner",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["ledger_id", "idempotency_key"],
            ["command_receipts.ledger_id", "command_receipts.key"],
            name=f"fk_{name}_receipt",
            deferrable=True,
            initially="DEFERRED",
        ),
    )


class SettlementIdentity:
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    ledger_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class BusinessPayment(SettlementIdentity, Base):
    __tablename__ = "business_payments"
    __table_args__ = (
        *_identity("business_payment"),
        *_journal("business_payment"),
        *_receipt("business_payment"),
        Index("ix_business_payments_ledger_created", "ledger_id", "created_at", "id"),
        ForeignKeyConstraint(
            ["party_id", "ledger_id"],
            ["business_parties.id", "business_parties.ledger_id"],
            name="fk_business_payment_party",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["account_id", "asset_id", "ledger_id"],
            ["account_assets.account_id", "account_assets.asset_id", "account_assets.ledger_id"],
            name="fk_business_payment_account_asset",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["initial_allocation_id", "id", "ledger_id"],
            [
                "business_allocations.id",
                "business_allocations.payment_id",
                "business_allocations.ledger_id",
            ],
            name="fk_business_payment_initial_allocation",
            use_alter=True,
            deferrable=True,
            initially="DEFERRED",
        ),
        CheckConstraint("direction IN ('receipt', 'payout')", name="ck_business_payment_direction"),
        CheckConstraint(
            "amount > 0 AND amount < 100000000000000000000", name="ck_business_payment_amount"
        ),
        CheckConstraint(
            "transaction_date BETWEEN DATE '0001-01-01' AND DATE '9999-12-31'",
            name="ck_business_payment_date",
        ),
        CheckConstraint(
            "CASE WHEN jsonb_typeof(fees) = 'array' "
            "THEN jsonb_array_length(fees) <= 20 ELSE false END",
            name="ck_business_payment_fees",
        ),
    )
    party_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    account_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    asset_id: Mapped[str] = mapped_column(String(200), nullable=False)
    direction: Mapped[str] = mapped_column(String(8), nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    transaction_date: Mapped[date] = mapped_column(Date, nullable=False)
    description: Mapped[str] = mapped_column(String(2000), nullable=False)
    fees: Mapped[list[dict]] = mapped_column(JSONB(none_as_null=True), nullable=False)
    operation_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    journal_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    initial_allocation_id: Mapped[UUID | None] = mapped_column(Uuid, nullable=True)
    created_by: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)


class BusinessAllocation(SettlementIdentity, Base):
    __tablename__ = "business_allocations"
    __table_args__ = (
        *_identity("business_allocation"),
        *_receipt("business_allocation"),
        UniqueConstraint("id", "payment_id", "ledger_id", name="uq_business_allocation_scope"),
        Index("ix_business_allocations_payment", "payment_id", "created_at", "id"),
        Index("ix_business_allocations_ledger_created", "ledger_id", "created_at", "id"),
        ForeignKeyConstraint(
            ["payment_id", "ledger_id"],
            ["business_payments.id", "business_payments.ledger_id"],
            name="fk_business_allocation_payment",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "allocation_date BETWEEN DATE '0001-01-01' AND DATE '9999-12-31'",
            name="ck_business_allocation_date",
        ),
    )
    payment_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    allocation_date: Mapped[date] = mapped_column(Date, nullable=False)
    created_by: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)


class BusinessAllocationLine(SettlementIdentity, Base):
    __tablename__ = "business_allocation_lines"
    __table_args__ = (
        *_identity("business_allocation_line"),
        *_journal("business_allocation_line"),
        UniqueConstraint("allocation_id", "line_no", name="uq_business_allocation_line_position"),
        UniqueConstraint(
            "allocation_id", "document_line_id", name="uq_business_allocation_line_source"
        ),
        Index("ix_business_allocation_lines_payment", "payment_id"),
        Index("ix_business_allocation_lines_source", "document_line_id"),
        ForeignKeyConstraint(
            ["allocation_id", "payment_id", "ledger_id"],
            [
                "business_allocations.id",
                "business_allocations.payment_id",
                "business_allocations.ledger_id",
            ],
            name="fk_business_allocation_line_group",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["document_line_id", "document_id", "ledger_id"],
            [
                "business_document_lines.id",
                "business_document_lines.document_id",
                "business_document_lines.ledger_id",
            ],
            name="fk_business_allocation_line_source",
            ondelete="RESTRICT",
        ),
        CheckConstraint("line_no BETWEEN 1 AND 200", name="ck_business_allocation_line_position"),
        CheckConstraint(
            "payment_amount > 0 AND payment_amount < 100000000000000000000 AND "
            "document_amount > 0 AND document_amount < 100000000000000000000",
            name="ck_business_allocation_line_amounts",
        ),
    )
    id: Mapped[UUID] = mapped_column(
        Uuid, primary_key=True, server_default=text("gen_random_uuid()")
    )
    allocation_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    payment_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    document_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    document_line_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    line_no: Mapped[int] = mapped_column(Integer, nullable=False)
    payment_amount: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    document_amount: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    operation_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    journal_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
