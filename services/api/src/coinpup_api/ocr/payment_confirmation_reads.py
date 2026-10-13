"""Read the selected payment and current operation in one owned readonly snapshot."""

from pydantic import ValidationError
from sqlalchemy import select

from coinpup_api.business.settlement_balances import integrity
from coinpup_api.business.settlement_reads import BusinessSettlementReads
from coinpup_api.ledger.models import FinancialOperation

from .payment_confirmation_contracts import PaymentEvidenceTarget


class PaymentEvidenceReadService(BusinessSettlementReads):
    def get_target(self, owner_id, ledger_id, payment_id):
        with self._transaction(owner_id, read_only=True) as session:
            self._ledger(session, owner_id, ledger_id)
            detail = self._payment_detail(session, ledger_id, payment_id)
            operation = session.scalar(
                select(FinancialOperation).where(
                    FinancialOperation.id == detail.receipt.operation_id,
                    FinancialOperation.ledger_id == ledger_id,
                    FinancialOperation.created_by == owner_id,
                )
            )
            if operation is None or operation.kind != "payment":
                raise integrity()
            try:
                return PaymentEvidenceTarget(
                    payment=detail,
                    operation_version=operation.version,
                    operation_status=operation.status,
                )
            except ValidationError:
                raise integrity() from None
