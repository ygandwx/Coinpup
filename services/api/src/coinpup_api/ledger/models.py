"""Owned ledger structure; balances are derived from future posting records."""

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    Numeric,
    SmallInteger,
    String,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from coinpup_api.business import models as business_models  # noqa: F401
from coinpup_api.ledger import period_models as period_models  # noqa: F401
from coinpup_api.models import Base


class Versioned:
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class AssetRecord(Versioned, Base):
    __tablename__ = "assets"
    __table_args__ = (
        CheckConstraint("version > 0", name="ck_assets_version"),
        CheckConstraint("scale BETWEEN 0 AND 18", name="ck_assets_scale"),
        CheckConstraint(
            "(kind = 'fiat' AND code ~ '^[A-Z]{3}$' "
            "AND code NOT IN ('BTC', 'ETH', 'XMR') "
            "AND (code NOT IN ('USD', 'GBP', 'EUR', 'HKD', 'CNY') OR scale = 2) "
            "AND network IS NULL AND token_reference IS NULL AND asset_id = code) OR "
            "(kind = 'native' AND token_reference IS NULL AND asset_id = code "
            "AND network IS NOT NULL AND "
            "((code = 'BTC' AND network = 'bitcoin' AND scale = 8) OR "
            "(code = 'ETH' AND network = 'ethereum' AND scale = 18) OR "
            "(code = 'XMR' AND network = 'monero' AND scale = 12))) OR "
            "(kind = 'token' AND code IN ('USDT', 'USDC') "
            "AND network IS NOT NULL AND network ~ '^[a-z][a-z0-9_-]{0,63}$' "
            "AND token_reference IS NOT NULL "
            "AND token_reference ~ '^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$' "
            "AND asset_id = 'token:' || network || ':' || token_reference)",
            name="ck_assets_identity",
        ),
    )

    asset_id: Mapped[str] = mapped_column(String(200), primary_key=True)
    code: Mapped[str] = mapped_column(String(8), nullable=False)
    kind: Mapped[str] = mapped_column(String(8), nullable=False)
    scale: Mapped[int] = mapped_column(Integer, nullable=False)
    network: Mapped[str | None] = mapped_column(String(64), nullable=True)
    token_reference: Mapped[str | None] = mapped_column(String(128), nullable=True)
    enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )


class Entity(Versioned, Base):
    __tablename__ = "entities"
    __table_args__ = (
        UniqueConstraint("id", "owner_id", name="uq_entities_id_owner"),
        CheckConstraint("kind IN ('personal', 'company')", name="ck_entities_kind"),
        CheckConstraint("btrim(name) <> ''", name="ck_entities_name"),
        CheckConstraint("version > 0", name="ck_entities_version"),
        CheckConstraint("jsonb_typeof(details) = 'object'", name="ck_entities_details"),
        CheckConstraint(
            "country_code IS NULL OR country_code ~ '^[A-Z]{2}$'",
            name="ck_entities_country_code",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    owner_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("administrators.id", name="fk_entities_owner", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    kind: Mapped[str] = mapped_column(String(8), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    legal_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    country_code: Mapped[str | None] = mapped_column(String(2), nullable=True)
    region_code: Mapped[str | None] = mapped_column(String(16), nullable=True)
    company_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    registration_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    details: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    archived: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )


class Ledger(Versioned, Base):
    __tablename__ = "ledgers"
    __table_args__ = (
        ForeignKeyConstraint(
            ["entity_id", "owner_id"],
            ["entities.id", "entities.owner_id"],
            name="fk_ledgers_entity_owner",
            ondelete="RESTRICT",
        ),
        UniqueConstraint("entity_id", name="uq_ledgers_entity"),
        UniqueConstraint("id", "owner_id", name="uq_ledgers_id_owner"),
        CheckConstraint("version > 0", name="ck_ledgers_version"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    entity_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    owner_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, index=True)
    base_asset_id: Mapped[str] = mapped_column(
        String(200),
        ForeignKey("assets.asset_id", name="fk_ledgers_base_asset", ondelete="RESTRICT"),
        nullable=False,
    )


class Account(Versioned, Base):
    __tablename__ = "accounts"
    __table_args__ = (
        UniqueConstraint("id", "ledger_id", name="uq_accounts_id_ledger"),
        UniqueConstraint("ledger_id", "system_key", name="uq_accounts_ledger_system_key"),
        CheckConstraint(
            "(account_class = 'money' AND system_key IS NULL AND kind IS NOT NULL) OR "
            "(kind IS NULL AND system_key IS NOT NULL AND ("
            "(account_class = 'receivable' AND system_key = 'receivable.customer') OR "
            "(account_class = 'payable' AND system_key = 'payable.supplier') OR "
            "(account_class = 'intercompany' AND system_key IN "
            "('intercompany.receivable', 'intercompany.payable')) OR "
            "(account_class = 'advance' AND system_key IN ('advance.received', 'advance.paid'))))",
            name="ck_accounts_class_identity",
        ),
        CheckConstraint(
            "kind IN ('bank', 'cash', 'wechat', 'alipay', 'credit_card', "
            "'paypal', 'wise', 'stripe', 'crypto')",
            name="ck_accounts_kind",
        ),
        CheckConstraint("btrim(name) <> ''", name="ck_accounts_name"),
        CheckConstraint("version > 0", name="ck_accounts_version"),
        CheckConstraint("jsonb_typeof(details) = 'object'", name="ck_accounts_details"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    ledger_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("ledgers.id", name="fk_accounts_ledger", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    kind: Mapped[str | None] = mapped_column(String(16), nullable=True)
    account_class: Mapped[str] = mapped_column(
        String(16), nullable=False, default="money", server_default="money"
    )
    system_key: Mapped[str | None] = mapped_column(String(40), nullable=True)
    details: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    archived: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )


class AccountAsset(Base):
    __tablename__ = "account_assets"
    __table_args__ = (
        UniqueConstraint(
            "account_id", "asset_id", "ledger_id", name="uq_account_assets_account_asset_ledger"
        ),
        ForeignKeyConstraint(
            ["account_id", "ledger_id"],
            ["accounts.id", "accounts.ledger_id"],
            name="fk_account_assets_account_ledger",
            ondelete="RESTRICT",
        ),
    )

    account_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    asset_id: Mapped[str] = mapped_column(
        String(200),
        ForeignKey("assets.asset_id", name="fk_account_assets_asset", ondelete="RESTRICT"),
        primary_key=True,
    )
    ledger_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, index=True)
    enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )


class Category(Versioned, Base):
    __tablename__ = "categories"
    __table_args__ = (
        UniqueConstraint("id", "ledger_id", "kind", name="uq_categories_id_ledger_kind"),
        ForeignKeyConstraint(
            ["parent_id", "ledger_id", "kind"],
            ["categories.id", "categories.ledger_id", "categories.kind"],
            name="fk_categories_parent_ledger_kind",
            ondelete="RESTRICT",
        ),
        CheckConstraint("kind IN ('income', 'expense')", name="ck_categories_kind"),
        CheckConstraint("parent_id IS NULL OR parent_id <> id", name="ck_categories_parent"),
        CheckConstraint("btrim(name) <> ''", name="ck_categories_name"),
        CheckConstraint("version > 0", name="ck_categories_version"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    ledger_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("ledgers.id", name="fk_categories_ledger", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    name_en: Mapped[str | None] = mapped_column(String(200), nullable=True)
    kind: Mapped[str] = mapped_column(String(8), nullable=False)
    parent_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    template_key: Mapped[str | None] = mapped_column(String(64), nullable=True)
    archived: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )


class FinancialOperation(Versioned, Base):
    __tablename__ = "financial_operations"
    __table_args__ = (
        UniqueConstraint("id", "ledger_id", name="uq_financial_operations_id_ledger"),
        ForeignKeyConstraint(
            ["ledger_id", "created_by"],
            ["ledgers.id", "ledgers.owner_id"],
            name="fk_financial_operations_ledger_owner",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["current_journal_id", "id", "ledger_id"],
            ["journals.id", "journals.operation_id", "journals.ledger_id"],
            name="fk_financial_operations_current_journal",
            deferrable=True,
            initially="DEFERRED",
            use_alter=True,
        ),
        CheckConstraint(
            "kind IN ('opening', 'income', 'expense', 'transfer', 'exchange')",
            name="ck_financial_operations_kind",
        ),
        CheckConstraint("version > 0", name="ck_financial_operations_version"),
        CheckConstraint("status IN ('active', 'cancelled')", name="ck_financial_operations_status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    ledger_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, index=True)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="active", server_default="active"
    )
    current_journal_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    created_by: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)


class Journal(Base):
    __tablename__ = "journals"
    __table_args__ = (
        Index("ix_journals_ledger_transaction_date", "ledger_id", "transaction_date"),
        Index("ix_journals_ledger_recognition_date", "ledger_id", "recognition_date"),
        UniqueConstraint("id", "operation_id", "ledger_id", name="uq_journals_id_operation_ledger"),
        UniqueConstraint("id", "ledger_id", name="uq_journals_id_ledger"),
        UniqueConstraint(
            "operation_id",
            "operation_version",
            "journal_kind",
            name="uq_journals_operation_revision",
        ),
        UniqueConstraint("reverses_journal_id", name="uq_journals_reversal"),
        ForeignKeyConstraint(
            ["operation_id", "ledger_id"],
            ["financial_operations.id", "financial_operations.ledger_id"],
            name="fk_journals_operation_ledger",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["reverses_journal_id", "operation_id", "ledger_id"],
            ["journals.id", "journals.operation_id", "journals.ledger_id"],
            name="fk_journals_reverses_operation_ledger",
            ondelete="RESTRICT",
        ),
        CheckConstraint("operation_version > 0", name="ck_journals_operation_version"),
        CheckConstraint(
            "(operation_version = 1 AND revision_reason IS NULL) OR "
            "(operation_version > 1 AND revision_reason IS NOT NULL "
            "AND revision_reason = btrim(revision_reason) "
            "AND revision_reason ~ '[^[:space:]]')",
            name="ck_journals_revision_reason",
        ),
        CheckConstraint(
            "(journal_kind = 'posting' AND reverses_journal_id IS NULL) OR "
            "(journal_kind = 'reversal' AND reverses_journal_id IS NOT NULL)",
            name="ck_journals_kind",
        ),
        CheckConstraint(
            "reverses_journal_id IS NULL OR reverses_journal_id <> id",
            name="ck_journals_self_reversal",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    operation_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    ledger_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, index=True)
    operation_version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )
    journal_kind: Mapped[str] = mapped_column(
        String(16), nullable=False, default="posting", server_default="posting"
    )
    reverses_journal_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    transaction_date: Mapped[date] = mapped_column(Date, nullable=False)
    recognition_date: Mapped[date] = mapped_column(Date, nullable=False)
    description: Mapped[str] = mapped_column(
        String(2000), nullable=False, default="", server_default=""
    )
    revision_reason: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    sealed: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class JournalLine(Base):
    __tablename__ = "journal_lines"
    __table_args__ = (
        ForeignKeyConstraint(
            ["project_id", "ledger_id"],
            ["business_projects.id", "business_projects.ledger_id"],
            name="fk_journal_lines_project_ledger",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["party_id", "ledger_id"],
            ["business_parties.id", "business_parties.ledger_id"],
            name="fk_journal_lines_party_ledger",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["document_id", "ledger_id"],
            ["business_documents.id", "business_documents.ledger_id"],
            name="fk_journal_lines_document_ledger",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["document_line_id", "document_id", "ledger_id"],
            [
                "business_document_lines.id",
                "business_document_lines.document_id",
                "business_document_lines.ledger_id",
            ],
            name="fk_journal_lines_document_line",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["ledger_id", "dimension_owner_id"],
            ["ledgers.id", "ledgers.owner_id"],
            name="fk_journal_lines_dimension_owner",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["counterparty_entity_id", "dimension_owner_id"],
            ["entities.id", "entities.owner_id"],
            name="fk_journal_lines_counterparty_owner",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "document_line_id IS NULL OR document_id IS NOT NULL",
            name="ck_journal_lines_document_dimension",
        ),
        CheckConstraint(
            "(counterparty_entity_id IS NULL) = (dimension_owner_id IS NULL)",
            name="ck_journal_lines_owner_dimension",
        ),
        Index(
            "ix_journal_lines_account_balance",
            "ledger_id",
            "account_id",
            "asset_id",
            postgresql_include=["amount"],
            postgresql_where=text("role = 'account'"),
        ),
        UniqueConstraint("journal_id", "line_no", name="uq_journal_lines_number"),
        ForeignKeyConstraint(
            ["journal_id", "ledger_id"],
            ["journals.id", "journals.ledger_id"],
            name="fk_journal_lines_journal_ledger",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["account_id", "asset_id", "ledger_id"],
            ["account_assets.account_id", "account_assets.asset_id", "account_assets.ledger_id"],
            name="fk_journal_lines_account_asset_ledger",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["category_id", "ledger_id", "role"],
            ["categories.id", "categories.ledger_id", "categories.kind"],
            name="fk_journal_lines_category_ledger_kind",
            ondelete="RESTRICT",
        ),
        CheckConstraint("line_no > 0", name="ck_journal_lines_number"),
        CheckConstraint("component_no BETWEEN 0 AND 20", name="ck_journal_lines_component"),
        CheckConstraint(
            "(role = 'account' AND account_id IS NOT NULL AND category_id IS NULL) OR "
            "(role IN ('income', 'expense') AND account_id IS NULL AND category_id IS NOT NULL) OR "
            "(role IN ('equity', 'exchange') AND account_id IS NULL AND category_id IS NULL)",
            name="ck_journal_lines_role",
        ),
        CheckConstraint(
            "amount <> 0 AND amount > -100000000000000000000 AND amount < 100000000000000000000",
            name="ck_journal_lines_amount",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    journal_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, index=True)
    ledger_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, index=True)
    line_no: Mapped[int] = mapped_column(Integer, nullable=False)
    component_no: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    asset_id: Mapped[str] = mapped_column(
        String(200),
        ForeignKey("assets.asset_id", name="fk_journal_lines_asset", ondelete="RESTRICT"),
        nullable=False,
    )
    amount: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    account_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    category_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)

    project_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    party_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    counterparty_entity_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    document_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    document_line_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    dimension_owner_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)


class OpeningPosition(Base):
    __tablename__ = "opening_positions"
    __table_args__ = (
        UniqueConstraint("operation_id", name="uq_opening_positions_operation"),
        ForeignKeyConstraint(
            ["account_id", "asset_id", "ledger_id"],
            ["account_assets.account_id", "account_assets.asset_id", "account_assets.ledger_id"],
            name="fk_opening_positions_account_asset_ledger",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["operation_id", "ledger_id"],
            ["financial_operations.id", "financial_operations.ledger_id"],
            name="fk_opening_positions_operation_ledger",
            ondelete="RESTRICT",
        ),
    )

    account_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    asset_id: Mapped[str] = mapped_column(String(200), primary_key=True)
    ledger_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    operation_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)


class CommandReceipt(Base):
    __tablename__ = "command_receipts"
    __table_args__ = (
        ForeignKeyConstraint(
            ["operation_id", "ledger_id"],
            ["financial_operations.id", "financial_operations.ledger_id"],
            name="fk_command_receipts_operation_ledger",
            ondelete="RESTRICT",
        ),
        CheckConstraint("key <> ''", name="ck_command_receipts_key"),
        CheckConstraint("request_hash ~ '^[0-9a-f]{64}$'", name="ck_command_receipts_hash"),
        CheckConstraint("hash_version IN (1, 2)", name="ck_command_receipts_hash_version"),
        CheckConstraint("response_status BETWEEN 200 AND 299", name="ck_command_receipts_status"),
        CheckConstraint("jsonb_typeof(response) = 'object'", name="ck_command_receipts_response"),
    )

    ledger_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    hash_version: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default="1")
    response: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    response_status: Mapped[int] = mapped_column(Integer, nullable=False)
    operation_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
