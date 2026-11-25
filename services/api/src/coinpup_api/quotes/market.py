"""Refresh one directed pair into an owner's append-only cache."""

from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from coinpup_api.ledger.common import asset_definition
from coinpup_api.ledger.models import AssetRecord
from coinpup_api.ledger.service import LedgerError, LedgerService, _not_found
from coinpup_api.quotes.market_models import MarketQuoteRecord
from coinpup_api.quotes.providers import fetch_market_observation, open_market_response


def _catalog(session, asset_ids, *, lock=False):
    statement = select(AssetRecord).where(AssetRecord.asset_id.in_(asset_ids))
    if lock:
        statement = statement.order_by(AssetRecord.asset_id).with_for_update(read=True)
    definitions = {row.asset_id: asset_definition(row) for row in session.scalars(statement)}
    if set(definitions) != set(asset_ids):
        raise _not_found()
    return definitions


class MarketQuoteService(LedgerService):
    def refresh(
        self,
        owner,
        asset_id,
        display_asset_id,
        *,
        price_date=None,
        fetch=open_market_response,
        clock=lambda: datetime.now(UTC),
    ):
        pair = (asset_id, display_asset_id)
        with self._transaction(owner, read_only=True) as session:
            catalog = _catalog(session, pair)
        observation = fetch_market_observation(
            catalog[asset_id],
            catalog[display_asset_id],
            price_date=price_date,
            fetch=fetch,
            clock=clock,
        )
        if observation is None:
            return None

        facts = dict(
            owner_id=owner,
            asset_id=asset_id,
            display_asset_id=display_asset_id,
            price=observation.price,
            source=observation.source,
            price_date=observation.price_date,
            observed_at=observation.observed_at,
        )
        with self._transaction(owner, write=True, read_committed=True) as session:
            if _catalog(session, pair, lock=True) != catalog:
                raise LedgerError("quote_asset_conflict", 409, "The quote asset identity changed.")
            new = session.scalar(
                insert(MarketQuoteRecord)
                .values(id=uuid4(), fetched_at=observation.fetched_at, **facts)
                .on_conflict_do_nothing(constraint="uq_quote_observation_original")
                .returning(MarketQuoteRecord.id)
            )
            if new is not None:
                return new
            return session.execute(select(MarketQuoteRecord.id).filter_by(**facts)).scalar_one()
