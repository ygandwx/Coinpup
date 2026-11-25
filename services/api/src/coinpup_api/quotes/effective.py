"""Read manual and public market evidence in the caller's owned database snapshot."""

from collections import defaultdict
from datetime import UTC, datetime, time
from fractions import Fraction

from sqlalchemy import func, select

from coinpup_api.ledger import get_asset
from coinpup_api.ledger.service import LedgerError, LedgerService
from coinpup_api.quotes.market import _catalog
from coinpup_api.quotes.market_models import MarketQuoteRecord
from coinpup_api.quotes.models import ManualPriceRecord
from coinpup_api.quotes.schemas import (
    AutomaticPriceResponse,
    EffectivePricePage,
    EffectivePriceResponse,
    ManualPriceResponse,
)
from coinpup_api.quotes.valuation import DirectQuote


def _eligible(statement, model, owner, assets, display_id, dates):
    scope = statement.where(
        model.owner_id == owner,
        model.asset_id.in_(assets),
        model.display_asset_id == display_id,
    )
    if dates is not None:
        return scope.where(model.price_date.in_(dates))
    return scope.where(model.price_date <= datetime.now(UTC).date())


def _automatic_prices(session, owner, assets, display_id, dates):
    model = MarketQuoteRecord
    partitions = (model.asset_id, model.price_date) if dates is not None else (model.asset_id,)
    price_rank = func.dense_rank().over(
        partition_by=partitions,
        order_by=(model.price_date.desc(), model.observed_at.desc().nulls_last()),
    )
    ranked = _eligible(
        select(model.id, price_rank.label("price_rank")),
        model,
        owner,
        assets,
        display_id,
        dates,
    ).subquery()
    statement = select(model).join(ranked, ranked.c.id == model.id).where(ranked.c.price_rank == 1)
    groups = defaultdict(list)
    for row in session.scalars(statement):
        groups[(row.asset_id, row.price_date)].append(row)
    return {
        key: (
            AutomaticPriceResponse.model_validate(min(rows, key=lambda row: row.price))
            if len({(row.source, Fraction(row.price)) for row in rows}) == 1
            else None
        )
        for key, rows in groups.items()
    }


def price_choices(session, owner, assets, display_id, *, dates):
    """Return effective slots, including None for conflicting market evidence."""
    model = ManualPriceRecord
    if dates is None:
        newest = func.row_number().over(
            partition_by=model.asset_id, order_by=model.price_date.desc()
        )
        ranked = _eligible(
            select(model.id, newest.label("price_rank")), model, owner, assets, display_id, dates
        ).subquery()
        manual = select(model).join(ranked, ranked.c.id == model.id).where(ranked.c.price_rank == 1)
    else:
        manual = _eligible(select(model), model, owner, assets, display_id, dates)
    choices = _automatic_prices(session, owner, assets, display_id, dates)
    for record in session.scalars(manual):
        choices[(record.asset_id, record.price_date)] = ManualPriceResponse.model_validate(record)
    if dates is not None:
        return choices
    # Latest chooses the newest available day, applying the manual override on that day.
    return {asset: choices[(asset, max(slots))] for asset, slots in _days_by_asset(choices).items()}


def _days_by_asset(choices):
    days = defaultdict(list)
    for asset, day in choices:
        days[asset].append(day)
    return days


def valuation_quote(base, display, price):
    if isinstance(price, ManualPriceResponse):
        observed, fetched = None, price.updated_at
    else:
        observed, fetched = price.observed_at, price.fetched_at
    return DirectQuote(
        base,
        display,
        price.price,
        price.source,
        observed or datetime.combine(price.price_date, time.min, UTC),
        fetched,
    )


class EffectivePriceService(LedgerService):
    def page(self, owner, asset_ids, display_id, price_date):
        if len(set(asset_ids)) != len(asset_ids):
            raise LedgerError("invalid_quote_scope", 422, "Choose distinct assets.")
        display = get_asset(display_id)
        with self._transaction(owner, read_only=True) as session:
            _catalog(session, (*asset_ids, display_id))
            choices = price_choices(session, owner, asset_ids, display_id, dates={price_date})
            items = []
            for asset_id in asset_ids:
                price = choices.get((asset_id, price_date))
                state = "identity" if asset_id == display.asset_id else "missing"
                manual, automatic = None, None
                if isinstance(price, ManualPriceResponse):
                    state, manual = "manual", price
                elif isinstance(price, AutomaticPriceResponse):
                    state, automatic = "automatic", price
                items.append(
                    EffectivePriceResponse(
                        asset_id=asset_id,
                        state=state,
                        manual_price=manual,
                        automatic_price=automatic,
                    )
                )
            return EffectivePricePage(
                price_date=price_date, display_asset_id=display_id, items=items
            )
