"""Original same-asset transfer command preparation."""

from uuid import uuid4

from coinpup_api.ledger.common import _invalid_money
from coinpup_api.ledger.errors import MoneyError
from coinpup_api.ledger.money import Amount
from coinpup_api.ledger.posting_schemas import TransferCreate, TransferResponse
from coinpup_api.ledger.posting_storage import PostingLine, prepare_journal_lines
from coinpup_api.ledger.service import LedgerError


class TransferCommands:
    def post_transfer(
        self,
        owner_id,
        ledger_id,
        payload: TransferCreate,
        idempotency_key: str,
        *,
        raw_body: bytes | None = None,
    ):
        with self._command(
            owner_id, ledger_id, "transfer", payload, idempotency_key, raw_body=raw_body
        ) as (
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
