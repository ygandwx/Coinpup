"""Exact current posting reconstruction and operation-state queries."""

from pydantic import ValidationError
from sqlalchemy import select

from coinpup_api.ledger.common import asset_definition
from coinpup_api.ledger.errors import MoneyError
from coinpup_api.ledger.models import AssetRecord, FinancialOperation, Journal, JournalLine
from coinpup_api.ledger.money import Amount
from coinpup_api.ledger.posting_schemas import (
    CancellationInfo,
    ExchangeResponse,
    FeeResponse,
    OperationResponse,
    OperationState,
    SplitResponse,
    TransferResponse,
)
from coinpup_api.ledger.service import LedgerError, _not_found, _page


class PostingReaders:
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
        fees = PostingReaders._read_fees(session, lines)
        lines = [line for line in lines if line.component_no == 0]
        if operation.kind == "transfer":
            return PostingReaders._read_transfer(session, operation, journal, lines, fees)
        if operation.kind == "exchange":
            return PostingReaders._read_exchange(session, operation, journal, lines, fees)
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


def operation_state(session, operation):
    cancellation = None
    if operation.status == "cancelled":
        reversal = session.scalar(
            select(Journal).where(
                Journal.operation_id == operation.id,
                Journal.ledger_id == operation.ledger_id,
                Journal.operation_version == operation.version,
                Journal.journal_kind == "reversal",
            )
        )
        if reversal is None:
            raise LedgerError("ledger_integrity", 503, "The cancellation journal is missing.")
        cancellation = CancellationInfo(
            version=operation.version,
            reversal_journal_id=reversal.id,
            reason=reversal.revision_reason,
            recorded_at=reversal.created_at,
        )
    try:
        return OperationState(
            id=operation.id,
            ledger_id=operation.ledger_id,
            kind=operation.kind,
            version=operation.version,
            status=operation.status,
            latest_posting=PostingReaders._read_operation(session, operation),
            updated_at=operation.updated_at,
            cancellation=cancellation,
        )
    except ValidationError:
        raise LedgerError(
            "ledger_integrity", 503, "The operation revision state is inconsistent."
        ) from None
