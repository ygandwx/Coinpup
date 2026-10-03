"""Atomic exact-quantity postings, stable command replay and derived balances."""

import hashlib
import json
import re
from decimal import Decimal
from uuid import uuid4

from sqlalchemy import func, insert, select, tuple_

from coinpup_api.ledger.assets import AssetDefinition
from coinpup_api.ledger.errors import MoneyError
from coinpup_api.ledger.models import (
    Account,
    AccountAsset,
    AssetRecord,
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
from coinpup_api.ledger.posting_schemas import (
    BalanceResponse,
    ExpenseCreate,
    IncomeCreate,
    OpeningCreate,
    OperationResponse,
    SplitResponse,
)
from coinpup_api.ledger.posting_storage import PostingLine, prepare_journal_lines
from coinpup_api.ledger.service import LedgerError, LedgerService, _not_found, _page

_IDEMPOTENCY_KEY = re.compile(r"[!-~]{1,128}")


def command_hash(kind, ledger_id, payload) -> str:
    """Hash normalized JSON structure, retaining the exact original amount strings.

    Server-generated IDs are absent from the hash. Omitted optional IDs and explicit
    null both serialize to null; retrying with the returned ID is a different command.
    """
    body = {"kind": kind, "ledger_id": str(ledger_id), "payload": payload.model_dump(mode="json")}
    encoded = json.dumps(
        body, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


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


class PostingService(LedgerService):
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
            select(Account).where(Account.id == account_id, Account.ledger_id == ledger_id)
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

    def post_opening(self, owner_id, ledger_id, payload: OpeningCreate, idempotency_key: str):
        return self._post(owner_id, ledger_id, "opening", payload, idempotency_key)

    def post_income(self, owner_id, ledger_id, payload: IncomeCreate, idempotency_key: str):
        return self._post(owner_id, ledger_id, "income", payload, idempotency_key)

    def post_expense(self, owner_id, ledger_id, payload: ExpenseCreate, idempotency_key: str):
        return self._post(owner_id, ledger_id, "expense", payload, idempotency_key)

    def _post(self, owner_id, ledger_id, kind, payload, key):
        if not isinstance(key, str) or _IDEMPOTENCY_KEY.fullmatch(key) is None:
            raise LedgerError(
                "invalid_idempotency_key",
                422,
                "Use 1–128 visible ASCII characters for the command key.",
            )
        digest = command_hash(kind, ledger_id, payload)
        with self._transaction(owner_id) as session:
            _, entity = self._locked_ledger(session, owner_id, ledger_id)
            receipt = session.get(CommandReceipt, (ledger_id, key))
            if receipt is not None:
                if receipt.request_hash != digest:
                    raise LedgerError(
                        "idempotency_conflict",
                        409,
                        "This command key was used for a different request.",
                    )
                return OperationResponse.model_validate(receipt.response)
            if entity.archived:
                raise LedgerError("entity_archived", 409, "Restore the entity before posting.")
            record = self._assets(session, [payload.asset_id])[0]
            definition = asset_definition(record)
            self._account(session, ledger_id, payload.account_id, payload.asset_id)
            if kind == "opening" and session.get(
                OpeningPosition, (payload.account_id, payload.asset_id)
            ):
                raise LedgerError(
                    "opening_exists", 409, "An opening entry already exists for this account asset."
                )
            try:
                quantity = Amount.parse(payload.amount, definition)
                if quantity.minor_units == 0 or (kind != "opening" and quantity.minor_units < 0):
                    raise MoneyError(
                        "amount_positive",
                        "A non-zero opening or positive income/expense is required.",
                    )
                splits = (
                    []
                    if kind == "opening"
                    else self._splits(session, ledger_id, payload, kind, quantity)
                )
                account_quantity = -quantity if kind == "expense" else quantity
                current_units = self._current_units(
                    session, ledger_id, payload.account_id, definition
                )
                Amount(definition, current_units + account_quantity.minor_units)
                operation_id, journal_id = payload.id or uuid4(), uuid4()
                lines = [
                    PostingLine(
                        journal_id=journal_id,
                        ledger_id=ledger_id,
                        line_no=1,
                        role="account",
                        asset_id=payload.asset_id,
                        amount=account_quantity,
                        account_id=payload.account_id,
                    )
                ]
                if kind == "opening":
                    lines.append(
                        PostingLine(
                            journal_id=journal_id,
                            ledger_id=ledger_id,
                            line_no=2,
                            role="equity",
                            asset_id=payload.asset_id,
                            amount=-quantity,
                        )
                    )
                else:
                    for index, (split, split_amount) in enumerate(splits, 2):
                        lines.append(
                            PostingLine(
                                journal_id=journal_id,
                                ledger_id=ledger_id,
                                line_no=index,
                                role=kind,
                                asset_id=payload.asset_id,
                                amount=-split_amount if kind == "income" else split_amount,
                                category_id=split.category_id,
                            )
                        )
                parameters = prepare_journal_lines(lines, {payload.asset_id: definition})
            except MoneyError as error:
                raise _invalid_money(error) from None
            recognition_date = (
                payload.transaction_date if kind == "opening" else payload.recognition_date
            )
            now = session.scalar(select(func.clock_timestamp()))
            # All quantity and classification checks have completed before the first INSERT.
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
            if kind == "opening":
                session.execute(
                    insert(OpeningPosition).values(
                        account_id=payload.account_id,
                        asset_id=payload.asset_id,
                        ledger_id=ledger_id,
                        operation_id=operation_id,
                    )
                )
            response = OperationResponse(
                id=operation_id,
                ledger_id=ledger_id,
                journal_id=journal_id,
                kind=kind,
                version=1,
                account_id=payload.account_id,
                asset_id=payload.asset_id,
                amount=quantity.to_string(),
                transaction_date=payload.transaction_date,
                recognition_date=recognition_date,
                description=payload.description,
                splits=[
                    SplitResponse(category_id=item.category_id, amount=amount.to_string())
                    for item, amount in splits
                ],
                created_at=now,
            )
            session.execute(
                insert(CommandReceipt).values(
                    ledger_id=ledger_id,
                    key=key,
                    request_hash=digest,
                    response=response.model_dump(mode="json"),
                    response_status=201,
                    operation_id=operation_id,
                    created_at=now,
                )
            )
            return response

    @staticmethod
    def _read_operation(session, operation):
        journal = session.get(Journal, operation.current_journal_id)
        if journal is None or journal.ledger_id != operation.ledger_id:
            raise LedgerError("ledger_integrity", 503, "The journal reference is inconsistent.")
        lines = session.scalars(
            select(JournalLine)
            .where(
                JournalLine.journal_id == journal.id, JournalLine.ledger_id == operation.ledger_id
            )
            .order_by(JournalLine.line_no)
        ).all()
        account_lines = [item for item in lines if item.role == "account"]
        if len(account_lines) != 1:
            raise LedgerError(
                "ledger_integrity", 503, "The posting account reference is inconsistent."
            )
        account = account_lines[0]
        asset = session.get(AssetRecord, account.asset_id)
        if asset is None:
            raise LedgerError(
                "ledger_integrity", 503, "The posting asset reference is inconsistent."
            )
        definition = asset_definition(asset)
        try:
            amount = Amount.from_decimal(account.amount, definition)
            if operation.kind == "expense":
                amount = -amount
            splits = []
            for line in lines:
                if line.role in {"income", "expense"}:
                    split = Amount.from_decimal(line.amount, definition)
                    if line.role == "income":
                        split = -split
                    splits.append(
                        SplitResponse(category_id=line.category_id, amount=split.to_string())
                    )
        except MoneyError:
            raise LedgerError(
                "ledger_integrity", 503, "Stored ledger quantities are inconsistent."
            ) from None
        return OperationResponse(
            id=operation.id,
            ledger_id=operation.ledger_id,
            journal_id=journal.id,
            kind=operation.kind,
            version=operation.version,
            account_id=account.account_id,
            asset_id=account.asset_id,
            amount=amount.to_string(),
            transaction_date=journal.transaction_date,
            recognition_date=journal.recognition_date,
            description=journal.description,
            splits=splits,
            created_at=operation.created_at,
        )

    def get_operation(self, owner_id, ledger_id, operation_id):
        with self._transaction(owner_id) as session:
            self._ledger(session, owner_id, ledger_id)
            operation = session.scalar(
                select(FinancialOperation).where(
                    FinancialOperation.id == operation_id, FinancialOperation.ledger_id == ledger_id
                )
            )
            if operation is None:
                raise _not_found()
            return self._read_operation(session, operation)

    def list_operations(self, owner_id, ledger_id, limit=100, offset=0):
        _page(limit, offset)
        with self._transaction(owner_id) as session:
            self._ledger(session, owner_id, ledger_id)
            operations = session.scalars(
                select(FinancialOperation)
                .where(FinancialOperation.ledger_id == ledger_id)
                .order_by(FinancialOperation.created_at.desc(), FinancialOperation.id.desc())
                .limit(limit)
                .offset(offset)
            ).all()
            return [self._read_operation(session, operation) for operation in operations]

    def balances(self, owner_id, ledger_id, account_id=None, limit=100, offset=0):
        _page(limit, offset)
        with self._transaction(owner_id) as session:
            self._locked_ledger(session, owner_id, ledger_id)
            if (
                account_id is not None
                and session.scalar(
                    select(Account.id).where(
                        Account.id == account_id, Account.ledger_id == ledger_id
                    )
                )
                is None
            ):
                raise _not_found()
            statement = (
                select(AccountAsset, Account, AssetRecord)
                .join(Account, Account.id == AccountAsset.account_id)
                .join(AssetRecord, AssetRecord.asset_id == AccountAsset.asset_id)
                .where(AccountAsset.ledger_id == ledger_id, Account.ledger_id == ledger_id)
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
