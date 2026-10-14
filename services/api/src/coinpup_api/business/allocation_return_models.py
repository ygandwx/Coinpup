"""Append-only return groups and exact mappings to original applications."""

from datetime import date
from decimal import Decimal
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Date,
    ForeignKeyConstraint,
    Index,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from coinpup_api.business.settlement_models import SettlementIdentity, _identity, _journal, _receipt
from coinpup_api.models import Base


class BusinessAllocationReturn(SettlementIdentity, Base):
    __tablename__ = "business_allocation_returns"
    __table_args__ = (
        *_identity("business_allocation_return"),
        *_receipt("business_allocation_return"),
        UniqueConstraint(
            "id", "payment_id", "ledger_id", name="uq_business_allocation_return_scope"
        ),
        ForeignKeyConstraint(
            ["payment_id", "ledger_id"],
            ["business_payments.id", "business_payments.ledger_id"],
            name="fk_business_allocation_return_payment",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "return_date BETWEEN DATE '0001-01-01' AND DATE '9999-12-31'",
            name="ck_business_allocation_return_date",
        ),
        CheckConstraint("btrim(reason) <> ''", name="ck_business_allocation_return_reason"),
        Index("ix_business_allocation_returns_payment", "payment_id", "created_at", "id"),
        Index("ix_business_allocation_returns_ledger_created", "ledger_id", "created_at", "id"),
    )
    payment_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    return_date: Mapped[date] = mapped_column(Date, nullable=False)
    reason: Mapped[str] = mapped_column(String(2000), nullable=False)
    created_by: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)


class BusinessAllocationReturnLine(SettlementIdentity, Base):
    __tablename__ = "business_allocation_return_lines"
    __table_args__ = (
        *_identity("business_allocation_return_line"),
        *_journal("business_allocation_return_line"),
        UniqueConstraint(
            "return_id", "line_no", name="uq_business_allocation_return_line_position"
        ),
        UniqueConstraint(
            "return_id",
            "source_allocation_line_id",
            name="uq_business_allocation_return_line_source",
        ),
        UniqueConstraint(
            "source_allocation_line_id",
            "source_return_no",
            name="uq_business_allocation_return_line_source_order",
        ),
        ForeignKeyConstraint(
            ["return_id", "payment_id", "ledger_id"],
            [
                "business_allocation_returns.id",
                "business_allocation_returns.payment_id",
                "business_allocation_returns.ledger_id",
            ],
            name="fk_business_allocation_return_line_group",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["source_allocation_line_id", "ledger_id"],
            ["business_allocation_lines.id", "business_allocation_lines.ledger_id"],
            name="fk_business_allocation_return_line_source",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "line_no BETWEEN 1 AND 200", name="ck_business_allocation_return_line_position"
        ),
        CheckConstraint(
            "source_version >= 1", name="ck_business_allocation_return_line_source_version"
        ),
        CheckConstraint(
            "source_return_no >= 1", name="ck_business_allocation_return_line_source_order"
        ),
        CheckConstraint(
            "payment_amount > 0 AND payment_amount < 100000000000000000000 AND "
            "document_amount > 0 AND document_amount < 100000000000000000000",
            name="ck_business_allocation_return_line_amounts",
        ),
        Index("ix_business_allocation_return_lines_payment", "payment_id"),
        Index("ix_business_allocation_return_lines_source", "source_allocation_line_id"),
    )
    id: Mapped[UUID] = mapped_column(
        Uuid, primary_key=True, server_default=text("gen_random_uuid()")
    )
    return_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    payment_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    source_allocation_line_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    source_version: Mapped[int] = mapped_column(Integer, nullable=False)
    source_return_no: Mapped[int] = mapped_column(BigInteger, nullable=False)
    line_no: Mapped[int] = mapped_column(Integer, nullable=False)
    payment_amount: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    document_amount: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    operation_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    journal_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
