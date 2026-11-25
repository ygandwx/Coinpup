"""Read and update manual prices in exact, owner-scoped transactions."""

from uuid import uuid4

from sqlalchemy import select

from coinpup_api.ledger.models import AssetRecord
from coinpup_api.ledger.service import LedgerError, LedgerService, _not_found, _page, _touch
from coinpup_api.quotes.models import ManualPriceRecord
from coinpup_api.quotes.schemas import ManualPricePage, ManualPriceResponse


def manual_prices_for_day(owner, price_date, display_asset_id):
    return select(ManualPriceRecord).where(
        ManualPriceRecord.owner_id == owner,
        ManualPriceRecord.price_date == price_date,
        ManualPriceRecord.display_asset_id == display_asset_id,
    )


class ManualPriceService(LedgerService):
    def page(self, owner, price_date, display_asset_id, *, asset_id=None, limit=100, offset=0):
        _page(limit, offset)
        with self._transaction(owner, read_only=True) as session:
            statement = manual_prices_for_day(owner, price_date, display_asset_id)
            if asset_id is not None:
                statement = statement.where(ManualPriceRecord.asset_id == asset_id)
            rows = session.scalars(
                statement.order_by(ManualPriceRecord.asset_id).limit(limit + 1).offset(offset)
            ).all()
            return ManualPricePage(
                items=tuple(ManualPriceResponse.model_validate(row) for row in rows[:limit]),
                limit=limit,
                offset=offset,
                has_more=len(rows) > limit,
            )

    def put(self, owner, payload):
        with self._transaction(owner, write=True, read_committed=True) as session:
            # Shared locks preserve catalog identity. Disabling posting for an asset
            # must not make its historical quantities impossible to value.
            assets = session.scalars(
                select(AssetRecord.asset_id)
                .where(AssetRecord.asset_id.in_((payload.asset_id, payload.display_asset_id)))
                .order_by(AssetRecord.asset_id)
                .with_for_update(read=True)
            ).all()
            if len(assets) != 2:
                raise _not_found()

            row = session.scalar(
                manual_prices_for_day(owner, payload.price_date, payload.display_asset_id)
                .where(ManualPriceRecord.asset_id == payload.asset_id)
                .with_for_update()
            )
            version = row.version if row else 0
            if (
                row is not None
                and payload.expected_version in (version, version - 1)
                and (row.price, row.source) == (payload.price, payload.source)
            ):
                return ManualPriceResponse.model_validate(row)
            if payload.expected_version != version:
                raise LedgerError(
                    "version_conflict", 409, "The price changed; reload before editing."
                )
            if row is None:
                row = ManualPriceRecord(
                    id=uuid4(),
                    owner_id=owner,
                    **payload.model_dump(exclude={"expected_version"}),
                    version=1,
                )
                session.add(row)
            else:
                row.price, row.source = payload.price, payload.source
                _touch(row)
            session.flush()
            return ManualPriceResponse.model_validate(row)
