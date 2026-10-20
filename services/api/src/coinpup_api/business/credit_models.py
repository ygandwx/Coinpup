"""Immutable credit groups and manual components bound to sealed document sources."""

from datetime import date
from decimal import Decimal
from uuid import UUID

from sqlalchemy import (
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


class BusinessCredit(SettlementIdentity, Base):
    __tablename__ = "business_credits"
    __table_args__ = (
        *_identity("business_credit"),
        *_receipt("business_credit"),
        UniqueConstraint("id", "document_id", "ledger_id", name="uq_business_credit_scope"),
        ForeignKeyConstraint(
            ["document_id", "ledger_id"],
            ["business_documents.id", "business_documents.ledger_id"],
            name="fk_business_credit_document",
            ondelete="RESTRICT",
        ),
        CheckConstraint("source_version >= 1", name="ck_business_credit_source_version"),
        CheckConstraint(
            "credit_date BETWEEN DATE '0001-01-01' AND DATE '9999-12-31'",
            name="ck_business_credit_date",
        ),
        CheckConstraint("btrim(reason) <> ''", name="ck_business_credit_reason"),
        Index("ix_business_credits_ledger_created", "ledger_id", "created_at", "id"),
        Index("ix_business_credits_document", "document_id", "created_at", "id"),
    )
    document_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    source_version: Mapped[int] = mapped_column(Integer, nullable=False)
    credit_date: Mapped[date] = mapped_column(Date, nullable=False)
    reason: Mapped[str] = mapped_column(String(2000), nullable=False)
    created_by: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)


class BusinessCreditLine(SettlementIdentity, Base):
    __tablename__ = "business_credit_lines"
    __table_args__ = (
        *_identity("business_credit_line"),
        *_journal("business_credit_line"),
        UniqueConstraint("credit_id", "line_no", name="uq_business_credit_line_position"),
        UniqueConstraint("credit_id", "source_line_id", name="uq_business_credit_line_source"),
        ForeignKeyConstraint(
            ["credit_id", "document_id", "ledger_id"],
            ["business_credits.id", "business_credits.document_id", "business_credits.ledger_id"],
            name="fk_business_credit_line_group",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["source_line_id", "document_id", "ledger_id"],
            [
                "business_document_lines.id",
                "business_document_lines.document_id",
                "business_document_lines.ledger_id",
            ],
            name="fk_business_credit_line_source",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["source_operation_id", "ledger_id"],
            ["financial_operations.id", "financial_operations.ledger_id"],
            name="fk_business_credit_line_original",
            ondelete="RESTRICT",
        ),
        CheckConstraint("line_no BETWEEN 1 AND 200", name="ck_business_credit_line_position"),
        CheckConstraint(
            "net_amount >= 0 AND tax_amount >= 0 AND "
            "total_amount > 0 AND total_amount < 100000000000000000000 AND "
            "net_amount + tax_amount = total_amount",
            name="ck_business_credit_line_amounts",
        ),
        CheckConstraint(
            "recognition_date BETWEEN DATE '0001-01-01' AND DATE '9999-12-31'",
            name="ck_business_credit_line_date",
        ),
        Index("ix_business_credit_lines_source", "source_line_id"),
    )
    id: Mapped[UUID] = mapped_column(
        Uuid, primary_key=True, server_default=text("gen_random_uuid()")
    )
    credit_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    document_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    source_line_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    source_operation_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    line_no: Mapped[int] = mapped_column(Integer, nullable=False)
    net_amount: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    tax_amount: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    total_amount: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    recognition_date: Mapped[date] = mapped_column(Date, nullable=False)
    operation_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    journal_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
