"""Neutral posting locks, ownership checks and atomic persistence."""

from sqlalchemy import func, insert, select

from coinpup_api.ledger.assets import AssetDefinition
from coinpup_api.ledger.errors import MoneyError
from coinpup_api.ledger.models import (
    Account,
    AccountAsset,
    Category,
    CommandReceipt,
    Entity,
    FinancialOperation,
    Journal,
    JournalLine,
    Ledger,
    OpeningPosition,
)
from coinpup_api.ledger.money import Amount
from coinpup_api.ledger.service import LedgerError, LedgerService, _not_found


def asset_definition(record) -> AssetDefinition:
    return AssetDefinition(
        record.code,
        record.kind,
        record.scale,
        record.network,
        record.token_reference,
        record.enabled,
    )


def _invalid_money(error):
    return LedgerError(error.code, 422, "The quantity, precision or journal balance is invalid.")


class PostingCore(LedgerService):
    """Use the same owner transaction and ledger lock order as structure maintenance."""

    @staticmethod
    def _locked_ledger(session, owner_id, ledger_id):
        ledger = session.scalar(
            select(Ledger)
            .where(Ledger.id == ledger_id, Ledger.owner_id == owner_id)
            .with_for_update()
        )
        if ledger is None:
            raise _not_found()
        entity = session.scalar(
            select(Entity)
            .where(Entity.id == ledger.entity_id, Entity.owner_id == owner_id)
            .with_for_update()
        )
        if entity is None:
            raise _not_found()
        return ledger, entity

    @staticmethod
    def _account(session, ledger_id, account_id, asset_id):
        account = session.scalar(
            select(Account).where(
                Account.id == account_id,
                Account.ledger_id == ledger_id,
                Account.account_class == "money",
            )
        )
        if account is None:
            raise _not_found()
        if account.archived:
            raise LedgerError("account_archived", 409, "Restore the account before posting.")
        link = session.get(AccountAsset, (account_id, asset_id))
        if link is None or link.ledger_id != ledger_id:
            raise _not_found()
        if not link.enabled:
            raise LedgerError(
                "account_asset_disabled", 409, "Enable this account asset before posting."
            )
        return account

    @staticmethod
    def _splits(session, ledger_id, payload, kind, quantity):
        requested = payload.splits
        identifiers = [item.category_id for item in requested]
        categories = session.scalars(
            select(Category).where(Category.ledger_id == ledger_id, Category.id.in_(identifiers))
        ).all()
        if len(categories) != len(identifiers):
            raise _not_found()
        if any(category.kind != kind for category in categories):
            raise LedgerError(
                "category_kind_mismatch", 422, "The category kind does not match this posting."
            )
        if any(category.archived for category in categories):
            raise LedgerError("category_archived", 409, "Restore the category before posting.")
        amounts = [Amount.parse(item.amount, quantity.asset) for item in requested]
        if any(item.minor_units <= 0 for item in amounts):
            raise MoneyError("amount_positive", "Every category split must be positive.")
        if sum(item.minor_units for item in amounts) != quantity.minor_units:
            raise MoneyError(
                "split_total_mismatch", "Category splits must equal the posting amount."
            )
        return list(zip(requested, amounts, strict=True))

    def _catalog(self, session, principal_assets, fees):
        identifiers = sorted(set(principal_assets) | {fee.asset_id for fee in fees})
        return {
            record.asset_id: asset_definition(record)
            for record in self._assets(session, identifiers)
        }

    @staticmethod
    def _persist(
        session,
        owner_id,
        ledger_id,
        kind,
        payload,
        key,
        digest,
        operation_id,
        journal_id,
        parameters,
        recognition_date,
        response_type,
        response_fields,
        *,
        opening_account_id=None,
    ):
        # Every caller completes both balance and row-binding validation before the first INSERT.
        now = session.scalar(select(func.clock_timestamp()))
        session.execute(
            insert(FinancialOperation).values(
                id=operation_id,
                ledger_id=ledger_id,
                kind=kind,
                version=1,
                current_journal_id=journal_id,
                created_by=owner_id,
                created_at=now,
                updated_at=now,
            )
        )
        session.execute(
            insert(Journal).values(
                id=journal_id,
                operation_id=operation_id,
                ledger_id=ledger_id,
                operation_version=1,
                journal_kind="posting",
                transaction_date=payload.transaction_date,
                recognition_date=recognition_date,
                description=payload.description,
                created_at=now,
            )
        )
        session.execute(insert(JournalLine), parameters)
        if opening_account_id is not None:
            session.execute(
                insert(OpeningPosition).values(
                    account_id=opening_account_id,
                    asset_id=payload.asset_id,
                    ledger_id=ledger_id,
                    operation_id=operation_id,
                )
            )
        response = response_type(
            id=operation_id,
            ledger_id=ledger_id,
            journal_id=journal_id,
            kind=kind,
            version=1,
            transaction_date=payload.transaction_date,
            recognition_date=recognition_date,
            description=payload.description,
            created_at=now,
            **response_fields,
        )
        session.execute(
            insert(CommandReceipt).values(
                ledger_id=ledger_id,
                key=key,
                request_hash=digest,
                hash_version=2,
                response=response.model_dump(mode="json"),
                response_status=201,
                operation_id=operation_id,
                created_at=now,
            )
        )
        return response
