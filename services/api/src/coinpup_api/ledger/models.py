"""Owned ledger structure; balances are derived from future posting records."""

import uuid
from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
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
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    details: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    archived: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )


class AccountAsset(Base):
    __tablename__ = "account_assets"
    __table_args__ = (
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
