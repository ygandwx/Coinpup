"""Original opening, income and expense command preparation."""

from uuid import uuid4

from coinpup_api.ledger.common import _invalid_money
from coinpup_api.ledger.errors import MoneyError
from coinpup_api.ledger.models import OpeningPosition
from coinpup_api.ledger.money import Amount
from coinpup_api.ledger.posting_schemas import (
    ExpenseCreate,
    IncomeCreate,
    OpeningCreate,
    OperationResponse,
    SplitResponse,
)
from coinpup_api.ledger.posting_storage import PostingLine, prepare_journal_lines
from coinpup_api.ledger.service import LedgerError


class ClassifiedCommands:
    def post_opening(
        self,
        owner_id,
        ledger_id,
        payload: OpeningCreate,
        idempotency_key: str,
        *,
        raw_body: bytes | None = None,
    ):
        return self._post(
            owner_id, ledger_id, "opening", payload, idempotency_key, raw_body=raw_body
        )

    def post_income(
        self,
        owner_id,
        ledger_id,
        payload: IncomeCreate,
        idempotency_key: str,
        *,
        raw_body: bytes | None = None,
    ):
        return self._post(
            owner_id, ledger_id, "income", payload, idempotency_key, raw_body=raw_body
        )

    def post_expense(
        self,
        owner_id,
        ledger_id,
        payload: ExpenseCreate,
        idempotency_key: str,
        *,
        raw_body: bytes | None = None,
    ):
        return self._post(
            owner_id, ledger_id, "expense", payload, idempotency_key, raw_body=raw_body
        )

    def _post(self, owner_id, ledger_id, kind, payload, key, *, raw_body=None):
        with self._command(owner_id, ledger_id, kind, payload, key, raw_body=raw_body) as (
            session,
            digest,
            replay,
        ):
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
