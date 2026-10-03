"""Atomic exact-quantity postings, stable command replay and derived balances."""

import hashlib
import json
import re
from contextlib import contextmanager
from decimal import Decimal
from uuid import uuid4

from pydantic import TypeAdapter, ValidationError
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
    ExchangeCreate,
    ExchangeResponse,
    ExpenseCreate,
    FeeResponse,
    FinancialResponse,
    IncomeCreate,
    OpeningCreate,
    OperationResponse,
    SplitResponse,
    TransferCreate,
    TransferResponse,
)
from coinpup_api.ledger.posting_storage import PostingLine, prepare_journal_lines
from coinpup_api.ledger.service import LedgerError, LedgerService, _not_found, _page

_IDEMPOTENCY_KEY = re.compile(r"[!-~]{1,128}")
_RECEIPT = TypeAdapter(FinancialResponse)
_COMMON_LEGACY_FIELDS = {"id", "asset_id", "amount", "transaction_date", "description"}
_LEGACY_HASH_FIELDS = {
    "opening": _COMMON_LEGACY_FIELDS | {"account_id"},
    "income": _COMMON_LEGACY_FIELDS | {"account_id", "recognition_date", "splits"},
    "expense": _COMMON_LEGACY_FIELDS | {"account_id", "recognition_date", "splits"},
    "transfer": _COMMON_LEGACY_FIELDS | {"source_account_id", "destination_account_id"},
}


def command_hash(kind, ledger_id, payload) -> str:
    """Hash normalized JSON structure, retaining the exact original amount strings.

    Server-generated IDs are absent from the hash. Omitted optional IDs and explicit
    null both serialize to null; retrying with the returned ID is a different command.
    """
    serialized = payload.model_dump(mode="json")
    if kind in _LEGACY_HASH_FIELDS:
        # Freeze the published command projection: new optional defaults must not invalidate
        # receipts issued before fees existed. Only a non-empty extension changes the hash.
        serialized = {
            field: serialized[field] for field in _LEGACY_HASH_FIELDS[kind] if field in serialized
        }
        if getattr(payload, "fees", None):
            serialized["fees"] = [item.model_dump(mode="json") for item in payload.fees]
    body = {"kind": kind, "ledger_id": str(ledger_id), "payload": serialized}
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

    def _catalog(self, session, principal_assets, fees):
        identifiers = sorted(set(principal_assets) | {fee.asset_id for fee in fees})
        return {
            record.asset_id: asset_definition(record)
            for record in self._assets(session, identifiers)
        }

    def _append_fees(self, session, ledger_id, lines, fees, catalog):
        responses = []
        for component, fee in enumerate(fees, 1):
            self._account(session, ledger_id, fee.account_id, fee.asset_id)
            category = session.scalar(
                select(Category).where(
                    Category.id == fee.category_id, Category.ledger_id == ledger_id
                )
            )
            if category is None:
                raise _not_found()
            if category.kind != "expense":
                raise LedgerError(
                    "category_kind_mismatch", 422, "A fee requires an expense category."
                )
            if category.archived:
                raise LedgerError(
                    "category_archived", 409, "Restore the fee category before posting."
                )
            quantity = Amount.parse(fee.amount, catalog[fee.asset_id])
            if quantity.minor_units <= 0:
                raise MoneyError("amount_positive", "A fee amount must be positive.")
            first = len(lines) + 1
            lines.extend(
                [
                    PostingLine(
                        journal_id=lines[0].journal_id,
                        ledger_id=ledger_id,
                        line_no=first,
                        component_no=component,
                        role="account",
                        asset_id=fee.asset_id,
                        amount=-quantity,
                        account_id=fee.account_id,
                    ),
                    PostingLine(
                        journal_id=lines[0].journal_id,
                        ledger_id=ledger_id,
                        line_no=first + 1,
                        component_no=component,
                        role="expense",
                        asset_id=fee.asset_id,
                        amount=quantity,
                        category_id=fee.category_id,
                    ),
                ]
            )
            responses.append(
                FeeResponse(
                    account_id=fee.account_id,
                    asset_id=fee.asset_id,
                    amount=quantity.to_string(),
                    category_id=fee.category_id,
                )
            )
        return responses

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

    def post_opening(self, owner_id, ledger_id, payload: OpeningCreate, idempotency_key: str):
        return self._post(owner_id, ledger_id, "opening", payload, idempotency_key)

    def post_income(self, owner_id, ledger_id, payload: IncomeCreate, idempotency_key: str):
        return self._post(owner_id, ledger_id, "income", payload, idempotency_key)

    def post_expense(self, owner_id, ledger_id, payload: ExpenseCreate, idempotency_key: str):
        return self._post(owner_id, ledger_id, "expense", payload, idempotency_key)

    @contextmanager
    def _command(self, owner_id, ledger_id, kind, payload, key):
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
                try:
                    response = _RECEIPT.validate_python(receipt.response)
                except ValidationError:
                    raise LedgerError(
                        "ledger_integrity", 503, "The stored command receipt is inconsistent."
                    ) from None
                yield session, digest, response
                return
            if entity.archived:
                raise LedgerError("entity_archived", 409, "Restore the entity before posting.")
            yield session, digest, None

    def _post(self, owner_id, ledger_id, kind, payload, key):
        with self._command(owner_id, ledger_id, kind, payload, key) as (session, digest, replay):
            if replay is not None:
                return replay
            fees = getattr(payload, "fees", [])
            catalog = self._catalog(session, [payload.asset_id], fees)
            definition = catalog[payload.asset_id]
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
                fee_responses = self._append_fees(session, ledger_id, lines, fees, catalog)
                self._validate_balance_deltas(session, ledger_id, lines, catalog)
                parameters = prepare_journal_lines(lines, catalog)
            except MoneyError as error:
                raise _invalid_money(error) from None
            recognition_date = (
                payload.transaction_date if kind == "opening" else payload.recognition_date
            )
            return self._persist(
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
                OperationResponse,
                {
                    "account_id": payload.account_id,
                    "asset_id": payload.asset_id,
                    "amount": quantity.to_string(),
                    "fees": fee_responses,
                    "splits": [
                        SplitResponse(category_id=item.category_id, amount=amount.to_string())
                        for item, amount in splits
                    ],
                },
                opening_account_id=payload.account_id if kind == "opening" else None,
            )

    def post_transfer(self, owner_id, ledger_id, payload: TransferCreate, idempotency_key: str):
        with self._command(owner_id, ledger_id, "transfer", payload, idempotency_key) as (
            session,
            digest,
            replay,
        ):
            if replay is not None:
                return replay
            # The schema rejects self-transfers; retain the invariant at the service boundary.
            if payload.source_account_id == payload.destination_account_id:
                raise LedgerError("same_account", 422, "Transfer accounts must be different.")
            catalog = self._catalog(session, [payload.asset_id], payload.fees)
            definition = catalog[payload.asset_id]
            for account_id in (payload.source_account_id, payload.destination_account_id):
                self._account(session, ledger_id, account_id, payload.asset_id)
            try:
                quantity = Amount.parse(payload.amount, definition)
                if quantity.minor_units <= 0:
                    raise MoneyError("amount_positive", "A transfer amount must be positive.")
                operation_id, journal_id = payload.id or uuid4(), uuid4()
                lines = [
                    PostingLine(
                        journal_id=journal_id,
                        ledger_id=ledger_id,
                        line_no=index,
                        role="account",
                        asset_id=payload.asset_id,
                        account_id=account_id,
                        amount=amount,
                    )
                    for index, (account_id, amount) in enumerate(
                        [
                            (payload.source_account_id, -quantity),
                            (payload.destination_account_id, quantity),
                        ],
                        1,
                    )
                ]
                fee_responses = self._append_fees(session, ledger_id, lines, payload.fees, catalog)
                self._validate_balance_deltas(session, ledger_id, lines, catalog)
                parameters = prepare_journal_lines(lines, catalog)
            except MoneyError as error:
                raise _invalid_money(error) from None
            return self._persist(
                session,
                owner_id,
                ledger_id,
                "transfer",
                payload,
                idempotency_key,
                digest,
                operation_id,
                journal_id,
                parameters,
                payload.transaction_date,
                TransferResponse,
                {
                    "source_account_id": payload.source_account_id,
                    "destination_account_id": payload.destination_account_id,
                    "asset_id": payload.asset_id,
                    "amount": quantity.to_string(),
                    "fees": fee_responses,
                },
            )

    def post_exchange(self, owner_id, ledger_id, payload: ExchangeCreate, idempotency_key: str):
        with self._command(owner_id, ledger_id, "exchange", payload, idempotency_key) as (
            session,
            digest,
            replay,
        ):
            if replay is not None:
                return replay
            if payload.source_asset_id == payload.destination_asset_id:
                raise LedgerError("same_asset", 422, "Exchange assets must be different.")
            catalog = self._catalog(
                session, [payload.source_asset_id, payload.destination_asset_id], payload.fees
            )
            self._account(session, ledger_id, payload.source_account_id, payload.source_asset_id)
            self._account(
                session, ledger_id, payload.destination_account_id, payload.destination_asset_id
            )
            try:
                source = Amount.parse(payload.source_amount, catalog[payload.source_asset_id])
                destination = Amount.parse(
                    payload.destination_amount, catalog[payload.destination_asset_id]
                )
                if source.minor_units <= 0 or destination.minor_units <= 0:
                    raise MoneyError("amount_positive", "Exchange quantities must be positive.")
                operation_id, journal_id = payload.id or uuid4(), uuid4()
                lines = [
                    PostingLine(
                        journal_id,
                        ledger_id,
                        1,
                        "account",
                        source.asset.asset_id,
                        -source,
                        account_id=payload.source_account_id,
                    ),
                    PostingLine(
                        journal_id, ledger_id, 2, "exchange", source.asset.asset_id, source
                    ),
                    PostingLine(
                        journal_id,
                        ledger_id,
                        3,
                        "account",
                        destination.asset.asset_id,
                        destination,
                        account_id=payload.destination_account_id,
                    ),
                    PostingLine(
                        journal_id,
                        ledger_id,
                        4,
                        "exchange",
                        destination.asset.asset_id,
                        -destination,
                    ),
                ]
                fee_responses = self._append_fees(session, ledger_id, lines, payload.fees, catalog)
                self._validate_balance_deltas(session, ledger_id, lines, catalog)
                parameters = prepare_journal_lines(lines, catalog)
            except MoneyError as error:
                raise _invalid_money(error) from None
            return self._persist(
                session,
                owner_id,
                ledger_id,
                "exchange",
                payload,
                idempotency_key,
                digest,
                operation_id,
                journal_id,
                parameters,
                payload.transaction_date,
                ExchangeResponse,
                {
                    "source_account_id": payload.source_account_id,
                    "source_asset_id": payload.source_asset_id,
                    "source_amount": source.to_string(),
                    "destination_account_id": payload.destination_account_id,
                    "destination_asset_id": payload.destination_asset_id,
                    "destination_amount": destination.to_string(),
                    "fees": fee_responses,
                },
            )

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
        fees = PostingService._read_fees(session, lines)
        lines = [line for line in lines if line.component_no == 0]
        if operation.kind == "transfer":
            return PostingService._read_transfer(session, operation, journal, lines, fees)
        if operation.kind == "exchange":
            return PostingService._read_exchange(session, operation, journal, lines, fees)
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
            version=journal.operation_version,
            account_id=account.account_id,
            asset_id=account.asset_id,
            amount=amount.to_string(),
            transaction_date=journal.transaction_date,
            recognition_date=journal.recognition_date,
            description=journal.description,
            splits=splits,
            fees=fees,
            created_at=journal.created_at,
        )

    @staticmethod
    def _read_transfer(session, operation, journal, lines, fees=None):
        def invalid():
            return LedgerError("ledger_integrity", 503, "The transfer journal is inconsistent.")

        if (
            len(lines) != 2
            or any(line.role != "account" or line.account_id is None for line in lines)
            or lines[0].account_id == lines[1].account_id
            or lines[0].asset_id != lines[1].asset_id
            or journal.recognition_date != journal.transaction_date
        ):
            raise invalid()
        asset = session.get(AssetRecord, lines[0].asset_id)
        if asset is None:
            raise invalid()
        definition = asset_definition(asset)
        try:
            decoded = [(line, Amount.from_decimal(line.amount, definition)) for line in lines]
        except MoneyError:
            raise invalid() from None
        source = [(line, amount) for line, amount in decoded if amount.minor_units < 0]
        destination = [(line, amount) for line, amount in decoded if amount.minor_units > 0]
        if len(source) != 1 or len(destination) != 1:
            raise invalid()
        if source[0][1].minor_units + destination[0][1].minor_units != 0:
            raise invalid()
        return TransferResponse(
            id=operation.id,
            ledger_id=operation.ledger_id,
            journal_id=journal.id,
            kind="transfer",
            version=journal.operation_version,
            source_account_id=source[0][0].account_id,
            destination_account_id=destination[0][0].account_id,
            asset_id=definition.asset_id,
            amount=destination[0][1].to_string(),
            transaction_date=journal.transaction_date,
            recognition_date=journal.recognition_date,
            description=journal.description,
            created_at=journal.created_at,
            fees=fees or [],
        )

    @staticmethod
    def _read_fees(session, lines):
        grouped = {}
        for line in lines:
            if line.component_no:
                grouped.setdefault(line.component_no, []).append(line)
        if sorted(grouped) != list(range(1, len(grouped) + 1)):
            raise LedgerError("ledger_integrity", 503, "Stored fee components are inconsistent.")
        result = []
        for number in sorted(grouped):
            component = grouped[number]
            accounts = [line for line in component if line.role == "account"]
            expenses = [line for line in component if line.role == "expense"]
            if (
                len(component) != 2
                or len(accounts) != 1
                or len(expenses) != 1
                or accounts[0].asset_id != expenses[0].asset_id
            ):
                raise LedgerError(
                    "ledger_integrity", 503, "Stored fee components are inconsistent."
                )
            account, expense = accounts[0], expenses[0]
            record = session.get(AssetRecord, account.asset_id)
            if record is None:
                raise LedgerError("ledger_integrity", 503, "Stored fee assets are inconsistent.")
            try:
                definition = asset_definition(record)
                debit = Amount.from_decimal(account.amount, definition)
                quantity = Amount.from_decimal(expense.amount, definition)
                if (
                    debit.minor_units >= 0
                    or quantity.minor_units <= 0
                    or debit.minor_units + quantity.minor_units
                ):
                    raise MoneyError("fee_invalid", "Fee quantities are inconsistent.")
            except MoneyError:
                raise LedgerError(
                    "ledger_integrity", 503, "Stored fee quantities are inconsistent."
                ) from None
            result.append(
                FeeResponse(
                    account_id=account.account_id,
                    asset_id=account.asset_id,
                    amount=quantity.to_string(),
                    category_id=expense.category_id,
                )
            )
        return result

    @staticmethod
    def _read_exchange(session, operation, journal, lines, fees):
        def invalid():
            return LedgerError("ledger_integrity", 503, "The exchange journal is inconsistent.")

        accounts = [line for line in lines if line.role == "account"]
        offsets = [line for line in lines if line.role == "exchange"]
        if (
            len(lines) != 4
            or len(accounts) != 2
            or len(offsets) != 2
            or accounts[0].asset_id == accounts[1].asset_id
            or journal.recognition_date != journal.transaction_date
        ):
            raise invalid()
        decoded = []
        for account in accounts:
            record = session.get(AssetRecord, account.asset_id)
            matched = [line for line in offsets if line.asset_id == account.asset_id]
            if record is None or len(matched) != 1:
                raise invalid()
            try:
                definition = asset_definition(record)
                quantity = Amount.from_decimal(account.amount, definition)
                offset = Amount.from_decimal(matched[0].amount, definition)
                if quantity.minor_units == 0 or quantity.minor_units + offset.minor_units:
                    raise invalid()
                decoded.append((account, quantity))
            except MoneyError:
                raise invalid() from None
        source = [item for item in decoded if item[1].minor_units < 0]
        destination = [item for item in decoded if item[1].minor_units > 0]
        if len(source) != 1 or len(destination) != 1:
            raise invalid()
        return ExchangeResponse(
            id=operation.id,
            ledger_id=operation.ledger_id,
            journal_id=journal.id,
            kind="exchange",
            version=journal.operation_version,
            source_account_id=source[0][0].account_id,
            source_asset_id=source[0][0].asset_id,
            source_amount=(-source[0][1]).to_string(),
            destination_account_id=destination[0][0].account_id,
            destination_asset_id=destination[0][0].asset_id,
            destination_amount=destination[0][1].to_string(),
            transaction_date=journal.transaction_date,
            recognition_date=journal.recognition_date,
            description=journal.description,
            created_at=journal.created_at,
            fees=fees,
        )

    def get_operation(self, owner_id, ledger_id, operation_id):
        from coinpup_api.ledger.revisions import operation_state

        with self._transaction(owner_id) as session:
            self._ledger(session, owner_id, ledger_id)
            operation = session.scalar(
                select(FinancialOperation).where(
                    FinancialOperation.id == operation_id, FinancialOperation.ledger_id == ledger_id
                )
            )
            if operation is None:
                raise _not_found()
            return operation_state(session, operation)

    def list_operations(self, owner_id, ledger_id, limit=100, offset=0, status="all"):
        from coinpup_api.ledger.revisions import operation_state

        _page(limit, offset)
        if status not in {"active", "cancelled", "all"}:
            raise LedgerError(
                "invalid_status", 422, "Select an active, cancelled or all status filter."
            )
        with self._transaction(owner_id) as session:
            self._ledger(session, owner_id, ledger_id)
            statement = select(FinancialOperation).where(FinancialOperation.ledger_id == ledger_id)
            if status != "all":
                statement = statement.where(FinancialOperation.status == status)
            operations = session.scalars(
                statement.order_by(
                    FinancialOperation.created_at.desc(), FinancialOperation.id.desc()
                )
                .limit(limit)
                .offset(offset)
            ).all()
            return [operation_state(session, operation) for operation in operations]

    def correct_operation(self, owner_id, ledger_id, operation_id, payload, idempotency_key):
        from coinpup_api.ledger.revisions import RevisionService

        return RevisionService(self.engine).correct(
            owner_id, ledger_id, operation_id, payload, idempotency_key
        )

    def cancel_operation(self, owner_id, ledger_id, operation_id, payload, idempotency_key):
        from coinpup_api.ledger.revisions import RevisionService

        return RevisionService(self.engine).cancel(
            owner_id, ledger_id, operation_id, payload, idempotency_key
        )

    def history(self, owner_id, ledger_id, operation_id, limit=100, offset=0):
        from coinpup_api.ledger.revisions import RevisionService

        return RevisionService(self.engine).read_history(
            owner_id, ledger_id, operation_id, limit, offset
        )

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
