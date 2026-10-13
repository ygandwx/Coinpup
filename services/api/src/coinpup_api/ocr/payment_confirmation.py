"""Evidence references immutable original receipts; replay never checks current business state."""

from pydantic import ValidationError
from sqlalchemy import select

from coinpup_api.business.settlement_models import BusinessPayment
from coinpup_api.business.settlement_reads import _payment_receipt
from coinpup_api.ledger.models import FinancialOperation
from coinpup_api.ledger.service import LedgerError, _not_found, _version

from .payment_confirmation_contracts import PaymentLinkConfirmationResponse


def payment_target(session, owner_id, ledger_id, entry):
    payment = session.scalar(
        select(BusinessPayment).where(
            BusinessPayment.id == entry.payment_id,
            BusinessPayment.ledger_id == ledger_id,
            BusinessPayment.created_by == owner_id,
        )
    )
    if payment is None:
        raise _not_found()
    operation = session.scalar(
        select(FinancialOperation)
        .where(
            FinancialOperation.id == payment.operation_id,
            FinancialOperation.ledger_id == ledger_id,
            FinancialOperation.created_by == owner_id,
            FinancialOperation.kind == "payment",
        )
        .with_for_update()
    )
    if operation is None:
        raise _not_found()
    _version(operation, entry.expected_version)
    if operation.status != "active":
        raise LedgerError("operation_cancelled", 409, "Choose an active financial operation.")
    return _payment_receipt(session, ledger_id, payment), operation.version


def replay_payment(receipt, session):
    try:
        response = PaymentLinkConfirmationResponse.model_validate(receipt.response)
        if session is None or (
            response.intent_id != receipt.intent_id
            or response.draft_id != receipt.draft_id
            or response.ledger_id != receipt.ledger_id
            or response.draft_version != receipt.draft_version + 1
            or response.target_ledger_id != receipt.target_ledger_id
            or response.target_file_id != receipt.target_file_id
            or response.action != receipt.action
            or response.payment.id != receipt.payment_id
            or response.payment.operation_id != receipt.operation_id
            or response.operation_version != receipt.operation_version
        ):
            raise ValueError
        # Only permanently immutable source rows are compared; never current status/master data.
        payment = session.scalar(
            select(BusinessPayment).where(
                BusinessPayment.id == receipt.payment_id,
                BusinessPayment.ledger_id == receipt.target_ledger_id,
                BusinessPayment.created_by == receipt.created_by,
            )
        )
        if (
            payment is None
            or _payment_receipt(session, receipt.target_ledger_id, payment) != response.payment
        ):
            raise ValueError
        return response
    except (ValueError, ValidationError, LedgerError):
        raise LedgerError(
            "ocr_invalid_receipt", 503, "The confirmation receipt is inconsistent."
        ) from None
