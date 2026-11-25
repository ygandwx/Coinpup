"""Owner-scoped manual direct prices for an explicit calendar day."""

from datetime import date, datetime
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
    Uuid,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from coinpup_api.models import Base


class ManualPriceRecord(Base):
    __tablename__ = "manual_prices"
    __table_args__ = (
        UniqueConstraint(
            "owner_id",
            "price_date",
            "display_asset_id",
            "asset_id",
            name="uq_manual_price_pair_day",
        ),
        CheckConstraint(
            "display_asset_id IN ('USD', 'GBP', 'EUR', 'HKD', 'CNY') "
            "AND asset_id <> display_asset_id",
            name="ck_manual_price_pair",
        ),
        CheckConstraint(
            "price ~ '^(0|[1-9][0-9]{0,19})([.][0-9]{1,18})?$' AND price ~ '[1-9]'",
            name="ck_manual_price_value",
        ),
        CheckConstraint(
            "btrim(source) = source AND source <> '' AND source !~ '[[:cntrl:]]'",
            name="ck_manual_price_source",
        ),
        CheckConstraint("version > 0", name="ck_manual_price_version"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    owner_id: Mapped[UUID] = mapped_column(
        Uuid,
        ForeignKey("administrators.id", name="fk_manual_price_owner", ondelete="RESTRICT"),
        nullable=False,
    )
    asset_id: Mapped[str] = mapped_column(
        String(200),
        ForeignKey("assets.asset_id", name="fk_manual_price_base_asset", ondelete="RESTRICT"),
        nullable=False,
    )
    display_asset_id: Mapped[str] = mapped_column(
        String(200),
        ForeignKey("assets.asset_id", name="fk_manual_price_display_asset", ondelete="RESTRICT"),
        nullable=False,
    )
    price_date: Mapped[date] = mapped_column(Date, nullable=False)
    price: Mapped[str] = mapped_column(String(39), nullable=False)
    source: Mapped[str] = mapped_column(String(128), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.clock_timestamp()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.clock_timestamp()
    )


class ManualPriceAudit(Base):
    __tablename__ = "manual_price_audits"

    price_id: Mapped[UUID] = mapped_column(
        Uuid,
        ForeignKey("manual_prices.id", name="fk_manual_price_audit_head", ondelete="RESTRICT"),
        primary_key=True,
    )
    version: Mapped[int] = mapped_column(Integer, primary_key=True)
    price: Mapped[str] = mapped_column(String(39), nullable=False)
    source: Mapped[str] = mapped_column(String(128), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
