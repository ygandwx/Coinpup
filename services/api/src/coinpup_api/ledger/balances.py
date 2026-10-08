"""Exact snapshot balance reads and locked net-command quantity guards."""

from decimal import Decimal

from sqlalchemy import func, select, tuple_

from coinpup_api.ledger.common import asset_definition
from coinpup_api.ledger.errors import MoneyError
from coinpup_api.ledger.models import Account, AccountAsset, AssetRecord, JournalLine
from coinpup_api.ledger.money import Amount
from coinpup_api.ledger.posting_schemas import BalanceResponse
from coinpup_api.ledger.service import LedgerError, _not_found, _page


class BalanceQueries:
    @staticmethod
    def _current_units(session, ledger_id, account_id, definition):
        value = session.scalar(
            select(func.sum(JournalLine.amount)).where(
                JournalLine.ledger_id == ledger_id,
                JournalLine.account_id == account_id,
                JournalLine.asset_id == definition.asset_id,
                JournalLine.role == "account",
            )
        )
        try:
            return Amount.from_decimal(
                value if value is not None else Decimal(0), definition
            ).minor_units
        except MoneyError:
            raise LedgerError(
                "ledger_integrity", 503, "Stored ledger quantities are inconsistent."
            ) from None

    def _validate_balance_deltas(self, session, ledger_id, lines, catalog):
        deltas = {}
        for line in lines:
            if line.role == "account":
                key = (line.account_id, line.asset_id)
                deltas[key] = deltas.get(key, 0) + line.amount.minor_units
        # Check the net command, not transient ordering of principal and fee lines.
        for (account_id, asset_id), delta in deltas.items():
            definition = catalog[asset_id]
            current = self._current_units(session, ledger_id, account_id, definition)
            Amount(definition, current + delta)

    def balances(self, owner_id, ledger_id, account_id=None, limit=100, offset=0):
        _page(limit, offset)
        with self._transaction(owner_id, read_only=True) as session:
            self._ledger(session, owner_id, ledger_id)
            if (
                account_id is not None
                and session.scalar(
                    select(Account.id).where(
                        Account.id == account_id,
                        Account.ledger_id == ledger_id,
                        Account.account_class == "money",
                    )
                )
                is None
            ):
                raise _not_found()
            statement = (
                select(AccountAsset, Account, AssetRecord)
                .join(Account, Account.id == AccountAsset.account_id)
                .join(AssetRecord, AssetRecord.asset_id == AccountAsset.asset_id)
                .where(
                    AccountAsset.ledger_id == ledger_id,
                    Account.ledger_id == ledger_id,
                    Account.account_class == "money",
                )
            )
            if account_id is not None:
                statement = statement.where(Account.id == account_id)
            rows = session.execute(
                statement.order_by(Account.id, AssetRecord.asset_id).limit(limit).offset(offset)
            ).all()
            if not rows:
                return []
            pairs = [(account.id, asset.asset_id) for _, account, asset in rows]
            totals = {
                (account, asset): value
                for account, asset, value in session.execute(
                    select(
                        JournalLine.account_id, JournalLine.asset_id, func.sum(JournalLine.amount)
                    )
                    .where(
                        JournalLine.ledger_id == ledger_id,
                        JournalLine.role == "account",
                        tuple_(JournalLine.account_id, JournalLine.asset_id).in_(pairs),
                    )
                    .group_by(JournalLine.account_id, JournalLine.asset_id)
                )
            }
            result = []
            for link, account, asset in rows:
                try:
                    amount = Amount.from_decimal(
                        totals.get((account.id, asset.asset_id), Decimal(0)),
                        asset_definition(asset),
                    )
                except MoneyError:
                    raise LedgerError(
                        "ledger_integrity", 503, "Stored ledger quantities are inconsistent."
                    ) from None
                result.append(
                    BalanceResponse(
                        account_id=account.id,
                        asset_id=asset.asset_id,
                        amount=amount.to_string(),
                        account_archived=account.archived,
                        asset_enabled=asset.enabled,
                        link_enabled=link.enabled,
                    )
                )
            return result
