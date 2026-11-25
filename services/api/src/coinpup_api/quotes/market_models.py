"""Immutable, owner-scoped observations from the configured public quote sources."""

from datetime import date, datetime
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column

from coinpup_api.models import Base


class MarketQuoteRecord(Base):
    __tablename__ = "quote_observations"
    __table_args__ = (
        CheckConstraint(
            "display_asset_id IN ('USD', 'GBP', 'EUR', 'HKD', 'CNY') "
            "AND asset_id <> display_asset_id",
            name="ck_quote_observation_pair",
        ),
        CheckConstraint(
            "price ~ '^(0|[1-9][0-9]{0,19})([.][0-9]{1,18})?$' AND price ~ '[1-9]'",
            name="ck_quote_observation_value",
        ),
        CheckConstraint(
            "(source = 'Frankfurter / ECB' AND observed_at IS NULL) OR "
            "(source = 'Coinbase Exchange' AND observed_at IS NOT NULL)",
            name="ck_quote_observation_source_time",
        ),
        CheckConstraint(
            "isfinite(price_date) AND isfinite(fetched_at) "
            "AND price_date <= (fetched_at AT TIME ZONE 'UTC')::date "
            "AND (observed_at IS NULL OR "
            "(isfinite(observed_at) AND observed_at <= fetched_at "
            "AND price_date = (observed_at AT TIME ZONE 'UTC')::date))",
            name="ck_quote_observation_dates",
        ),
        UniqueConstraint(
            "owner_id",
            "asset_id",
            "display_asset_id",
            "source",
            "price_date",
            "observed_at",
            "price",
            name="uq_quote_observation_original",
            postgresql_nulls_not_distinct=True,
        ),
        Index(
            "ix_quote_observation_day",
            "owner_id",
            "display_asset_id",
            "asset_id",
            "price_date",
            "observed_at",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    owner_id: Mapped[UUID] = mapped_column(
        Uuid,
        ForeignKey("administrators.id", name="fk_quote_observation_owner", ondelete="RESTRICT"),
    )
    asset_id: Mapped[str] = mapped_column(
        String(200),
        ForeignKey("assets.asset_id", name="fk_quote_observation_asset", ondelete="RESTRICT"),
    )
    display_asset_id: Mapped[str] = mapped_column(
        String(200),
        ForeignKey("assets.asset_id", name="fk_quote_observation_display", ondelete="RESTRICT"),
    )
    price: Mapped[str] = mapped_column(String(39))
    source: Mapped[str] = mapped_column(String(128))
    price_date: Mapped[date] = mapped_column(Date)
    observed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
