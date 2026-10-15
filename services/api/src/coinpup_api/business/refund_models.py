"""Immutable actual refunds and explicitly ordered original-return references."""

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
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from coinpup_api.business.settlement_models import SettlementIdentity, _identity, _journal, _receipt
from coinpup_api.models import Base


class BusinessRefund(SettlementIdentity, Base):
    __tablename__ = "business_refunds"
    __table_args__ = (
        *_identity("business_refund"),
        *_journal("business_refund"),
        *_receipt("business_refund"),
        UniqueConstraint("id", "payment_id", "ledger_id", name="uq_business_refund_scope"),
        ForeignKeyConstraint(
            ["payment_id", "ledger_id"],
            ["business_payments.id", "business_payments.ledger_id"],
            name="fk_business_refund_payment",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["account_id", "asset_id", "ledger_id"],
            ["account_assets.account_id", "account_assets.asset_id", "account_assets.ledger_id"],
            name="fk_business_refund_account_asset",
            ondelete="RESTRICT",
        ),
        CheckConstraint("source_version >= 1", name="ck_business_refund_source_version"),
        CheckConstraint(
            "amount > 0 AND amount < 100000000000000000000 AND "
            "payment_amount > 0 AND payment_amount < 100000000000000000000",
            name="ck_business_refund_amounts",
        ),
        CheckConstraint(
            "transaction_date BETWEEN DATE '0001-01-01' AND DATE '9999-12-31'",
            name="ck_business_refund_date",
        ),
        CheckConstraint("btrim(reason) <> ''", name="ck_business_refund_reason"),
        CheckConstraint(
            "CASE WHEN jsonb_typeof(fees) = 'array' "
            "THEN jsonb_array_length(fees) <= 20 ELSE false END",
            name="ck_business_refund_fees",
        ),
        Index("ix_business_refunds_ledger_created", "ledger_id", "created_at", "id"),
        Index("ix_business_refunds_payment", "payment_id", "created_at", "id"),
    )
    payment_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    source_version: Mapped[int] = mapped_column(Integer, nullable=False)
    account_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    asset_id: Mapped[str] = mapped_column(String(200), nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    payment_amount: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    transaction_date: Mapped[date] = mapped_column(Date, nullable=False)
    reason: Mapped[str] = mapped_column(String(2000), nullable=False)
    fees: Mapped[list] = mapped_column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    created_by: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    operation_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    journal_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)


class BusinessRefundReturn(SettlementIdentity, Base):
    __tablename__ = "business_refund_returns"
    __table_args__ = (
        *_identity("business_refund_return"),
        UniqueConstraint("refund_id", "line_no", name="uq_business_refund_return_position"),
        UniqueConstraint("refund_id", "return_id", name="uq_business_refund_return_source"),
        ForeignKeyConstraint(
            ["refund_id", "payment_id", "ledger_id"],
            ["business_refunds.id", "business_refunds.payment_id", "business_refunds.ledger_id"],
            name="fk_business_refund_return_group",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["return_id", "payment_id", "ledger_id"],
            [
                "business_allocation_returns.id",
                "business_allocation_returns.payment_id",
                "business_allocation_returns.ledger_id",
            ],
            name="fk_business_refund_return_source",
            ondelete="RESTRICT",
        ),
        CheckConstraint("line_no BETWEEN 1 AND 200", name="ck_business_refund_return_position"),
        Index("ix_business_refund_returns_source", "return_id"),
    )
    id: Mapped[UUID] = mapped_column(
        Uuid, primary_key=True, server_default=text("gen_random_uuid()")
    )
    refund_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    payment_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    return_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    line_no: Mapped[int] = mapped_column(Integer, nullable=False)
