"""Original independent fee components and account/category checks."""

from sqlalchemy import select

from coinpup_api.ledger.errors import MoneyError
from coinpup_api.ledger.models import Category
from coinpup_api.ledger.money import Amount
from coinpup_api.ledger.posting_schemas import FeeResponse
from coinpup_api.ledger.posting_storage import PostingLine
from coinpup_api.ledger.service import LedgerError, _not_found


class FeeCommands:
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
