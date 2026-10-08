"""Internal control account acquisition and independent snapshot balance reads."""

from sqlalchemy import and_, func, select

from coinpup_api.ledger.common import asset_definition
from coinpup_api.ledger.control_schemas import CONTROL_CLASSES, ControlBalance
from coinpup_api.ledger.errors import MoneyError
from coinpup_api.ledger.models import Account, AccountAsset, AssetRecord, JournalLine
from coinpup_api.ledger.money import Amount
from coinpup_api.ledger.service import LedgerError, LedgerService, _page
from coinpup_api.sync.locking import acquire_write_lock

_DIMENSIONS = ("party_id", "counterparty_entity_id", "document_id", "document_line_id")


class ControlAccounts(LedgerService):
    def ensure(self, session, owner_id, ledger_id, requirements):
        """Join a business transaction before it locks assets; never commit or post money.

        The caller owns commit/rollback and its permanent command receipt. There is no
        public account-creation command for controls. Retrying adds no duplicate/version.
        """
        if not session.in_transaction():
            raise RuntimeError("Control acquisition requires a caller-owned transaction")
        requested = {key: set(assets) for key, assets in requirements.items()}
        if not requested or any(
            key not in CONTROL_CLASSES or not ids for key, ids in requested.items()
        ):
            raise LedgerError("invalid_control", 422, "Use a known control identity and assets.")
        acquire_write_lock(session)
        self._ledger(session, owner_id, ledger_id, write=True)
        self._assets(session, set().union(*requested.values()))
        accounts = {
            a.system_key: a
            for a in session.scalars(
                select(Account).where(
                    Account.ledger_id == ledger_id, Account.system_key.in_(sorted(requested))
                )
            )
        }
        if any(account.archived for account in accounts.values()):
            raise LedgerError("account_archived", 409, "Restore the account before new use.")
        links = {
            (link.account_id, link.asset_id): link
            for link in session.scalars(
                select(AccountAsset).where(
                    AccountAsset.ledger_id == ledger_id,
                    AccountAsset.account_id.in_([a.id for a in accounts.values()]),
                )
            )
        }
        for key, account in accounts.items():
            if any(
                (link := links.get((account.id, asset))) is not None and not link.enabled
                for asset in requested[key]
            ):
                raise LedgerError(
                    "account_asset_disabled", 409, "Restore the asset link before new use."
                )
        for key in sorted(requested):
            account = accounts.get(key)
            created = account is None
            if created:
                account = Account(
                    ledger_id=ledger_id,
                    name=key,
                    kind=None,
                    account_class=CONTROL_CLASSES[key],
                    system_key=key,
                )
                session.add(account)
                session.flush()
                accounts[key] = account
            missing = [
                asset for asset in sorted(requested[key]) if (account.id, asset) not in links
            ]
            if missing and not created:
                account.version += 1
                account.updated_at = func.now()
                session.flush()
            for asset in missing:
                session.add(
                    AccountAsset(account_id=account.id, ledger_id=ledger_id, asset_id=asset)
                )
            session.flush()
        return accounts

    def balances(
        self,
        owner_id,
        ledger_id,
        *,
        account_class=None,
        system_key=None,
        party_id=None,
        counterparty_entity_id=None,
        document_id=None,
        document_line_id=None,
        limit=100,
        offset=0,
    ):
        _page(limit, offset)
        if (
            account_class is not None
            and account_class not in CONTROL_CLASSES.values()
            or system_key is not None
            and system_key not in CONTROL_CLASSES
        ):
            raise LedgerError("invalid_control", 422, "Use a known control class or identity.")
        with self._transaction(owner_id, read_only=True) as session:
            self._ledger(session, owner_id, ledger_id)
            dimensions = [getattr(JournalLine, name) for name in _DIMENSIONS]
            statement = (
                select(
                    Account,
                    AccountAsset.enabled,
                    AssetRecord,
                    *dimensions,
                    func.coalesce(func.sum(JournalLine.amount), 0),
                )
                .select_from(Account)
                .join(
                    AccountAsset,
                    and_(
                        AccountAsset.account_id == Account.id,
                        AccountAsset.ledger_id == Account.ledger_id,
                    ),
                )
                .join(AssetRecord, AssetRecord.asset_id == AccountAsset.asset_id)
                .outerjoin(
                    JournalLine,
                    and_(
                        JournalLine.account_id == Account.id,
                        JournalLine.ledger_id == Account.ledger_id,
                        JournalLine.asset_id == AccountAsset.asset_id,
                        JournalLine.role == "account",
                    ),
                )
                .where(Account.ledger_id == ledger_id, Account.account_class != "money")
            )
            for column, value in (
                (Account.account_class, account_class),
                (Account.system_key, system_key),
                *zip(
                    dimensions,
                    (party_id, counterparty_entity_id, document_id, document_line_id),
                    strict=True,
                ),
            ):
                if value is not None:
                    statement = statement.where(column == value)
            statement = (
                statement.group_by(
                    Account.id, AccountAsset.enabled, AssetRecord.asset_id, *dimensions
                )
                .order_by(
                    Account.id,
                    AssetRecord.asset_id,
                    *(column.asc().nulls_first() for column in dimensions),
                )
                .limit(limit)
                .offset(offset)
            )
            result = []
            for account, enabled, asset, *values in session.execute(statement):
                try:
                    amount = Amount.from_decimal(values[-1], asset_definition(asset)).to_string()
                except MoneyError:
                    raise LedgerError(
                        "ledger_integrity", 503, "Stored control quantities are inconsistent."
                    ) from None
                result.append(
                    ControlBalance(
                        account_id=account.id,
                        account_class=account.account_class,
                        system_key=account.system_key,
                        asset_id=asset.asset_id,
                        amount=amount,
                        account_archived=account.archived,
                        asset_enabled=asset.enabled,
                        link_enabled=enabled,
                        **dict(zip(_DIMENSIONS, values[:-1], strict=True)),
                    )
                )
            return result
