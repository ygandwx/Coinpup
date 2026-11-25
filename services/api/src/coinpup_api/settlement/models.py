"""Owner linkage between two separately scoped, immutable ledger operations."""

from datetime import date
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from coinpup_api.models import Base


class InterledgerPreference(Base):
    __tablename__ = "interledger_settings"
    __table_args__ = (CheckConstraint("version > 0", name="ck_interledger_settings_version"),)

    owner_id: Mapped[UUID] = mapped_column(
        Uuid,
        ForeignKey("administrators.id", name="fk_interledger_settings_owner", ondelete="RESTRICT"),
        primary_key=True,
    )
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)


class InterledgerGroup(Base):
    __tablename__ = "interledger_groups"
    __table_args__ = (
        UniqueConstraint("owner_id", "command_key", name="uq_interledger_owner_command"),
        UniqueConstraint("payer_operation_id", name="uq_interledger_payer_operation"),
        UniqueConstraint("beneficiary_operation_id", name="uq_interledger_beneficiary_operation"),
        UniqueConstraint(
            "id",
            "owner_id",
            "payer_ledger_id",
            "beneficiary_ledger_id",
            "asset_id",
            "kind",
            name="uq_interledger_source_identity",
        ),
        ForeignKeyConstraint(
            ["payer_ledger_id", "owner_id"],
            ["ledgers.id", "ledgers.owner_id"],
            name="fk_interledger_payer_owner",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["beneficiary_ledger_id", "owner_id"],
            ["ledgers.id", "ledgers.owner_id"],
            name="fk_interledger_beneficiary_owner",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["payer_operation_id", "payer_ledger_id"],
            ["financial_operations.id", "financial_operations.ledger_id"],
            name="fk_interledger_payer_operation",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["beneficiary_operation_id", "beneficiary_ledger_id"],
            ["financial_operations.id", "financial_operations.ledger_id"],
            name="fk_interledger_beneficiary_operation",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["payer_account_id", "asset_id", "payer_ledger_id"],
            ["account_assets.account_id", "account_assets.asset_id", "account_assets.ledger_id"],
            name="fk_interledger_payer_account",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["beneficiary_account_id", "asset_id", "beneficiary_ledger_id"],
            ["account_assets.account_id", "account_assets.asset_id", "account_assets.ledger_id"],
            name="fk_interledger_beneficiary_account",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["expense_category_id", "beneficiary_ledger_id", "expense_kind"],
            ["categories.id", "categories.ledger_id", "categories.kind"],
            name="fk_interledger_expense_category",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            [
                "advance_id",
                "owner_id",
                "payer_ledger_id",
                "beneficiary_ledger_id",
                "asset_id",
                "advance_kind",
            ],
            [
                "interledger_groups.id",
                "interledger_groups.owner_id",
                "interledger_groups.payer_ledger_id",
                "interledger_groups.beneficiary_ledger_id",
                "interledger_groups.asset_id",
                "interledger_groups.kind",
            ],
            name="fk_interledger_advance",
            ondelete="RESTRICT",
        ),
        CheckConstraint("payer_ledger_id <> beneficiary_ledger_id", name="ck_interledger_pair"),
        CheckConstraint(
            "(kind = 'advance' AND advance_id IS NULL AND advance_kind IS NULL "
            "AND beneficiary_account_id IS NULL AND expense_category_id IS NOT NULL "
            "AND expense_kind IS NOT DISTINCT FROM 'expense') OR "
            "(kind = 'repayment' AND advance_id IS NOT NULL AND advance_id <> id "
            "AND advance_kind IS NOT DISTINCT FROM 'advance' "
            "AND beneficiary_account_id IS NOT NULL "
            "AND expense_category_id IS NULL AND expense_kind IS NULL)",
            name="ck_interledger_shape",
        ),
        CheckConstraint(
            "amount > 0 AND amount < 100000000000000000000", name="ck_interledger_amount"
        ),
        CheckConstraint("command_key ~ '^[!-~]{1,128}$'", name="ck_interledger_key"),
        CheckConstraint("request_hash ~ '^[0-9a-f]{64}$'", name="ck_interledger_request_hash"),
        CheckConstraint("jsonb_typeof(response) = 'object'", name="ck_interledger_response"),
        Index("ix_interledger_owner_date", "owner_id", "transaction_date", "id"),
        Index("ix_interledger_advance_date", "advance_id", "transaction_date", "id"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    owner_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    payer_ledger_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    beneficiary_ledger_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    payer_account_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    beneficiary_account_id: Mapped[UUID | None] = mapped_column(Uuid)
    expense_category_id: Mapped[UUID | None] = mapped_column(Uuid)
    expense_kind: Mapped[str | None] = mapped_column(String(8))
    advance_id: Mapped[UUID | None] = mapped_column(Uuid)
    advance_kind: Mapped[str | None] = mapped_column(String(16))
    payer_operation_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    beneficiary_operation_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    asset_id: Mapped[str] = mapped_column(String(200), nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    transaction_date: Mapped[date] = mapped_column(Date, nullable=False)
    description: Mapped[str] = mapped_column(String(2000), nullable=False)
    command_key: Mapped[str] = mapped_column(String(128), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    response: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
