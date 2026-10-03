"""Original multi-asset exchange command preparation."""

from uuid import uuid4

from coinpup_api.ledger.common import _invalid_money
from coinpup_api.ledger.errors import MoneyError
from coinpup_api.ledger.money import Amount
from coinpup_api.ledger.posting_schemas import ExchangeCreate, ExchangeResponse
from coinpup_api.ledger.posting_storage import PostingLine, prepare_journal_lines
from coinpup_api.ledger.service import LedgerError


class ExchangeCommands:
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
