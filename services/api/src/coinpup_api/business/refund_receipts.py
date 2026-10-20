"""Permanent refund receipts verified from immutable facts before current availability."""

from decimal import Decimal

from pydantic import ValidationError
from sqlalchemy import func, select

from coinpup_api.business.refund_models import BusinessRefund
from coinpup_api.business.refund_schemas import BusinessRefundResponse
from coinpup_api.ledger.idempotency import receipt_hash_version
from coinpup_api.ledger.service import LedgerError


def integrity():
    return LedgerError("refund_integrity", 503, "The stored refund is inconsistent.")


def response_from_rows(session, identifier):
    try:
        return BusinessRefundResponse.model_validate(
            session.scalar(select(func.coinpup_business_refund_response(identifier)))
        )
    except ValidationError:
        raise integrity() from None


def replay_refund(session, receipt, ledger_id, payload, digest):
    if receipt_hash_version(receipt) != 2 or receipt.request_hash != digest:
        raise LedgerError("idempotency_conflict", 409, "This key belongs to a different request.")
    saved = session.scalar(
        select(BusinessRefund).where(
            BusinessRefund.id == payload.id,
            BusinessRefund.ledger_id == ledger_id,
            BusinessRefund.idempotency_key == receipt.key,
        )
    )
    if (
        saved is None
        or saved.payment_id != payload.payment_id
        or saved.source_version != payload.expected_version
        or saved.account_id != payload.account_id
        or saved.asset_id != payload.asset_id
        or saved.amount != Decimal(payload.amount)
        or saved.payment_amount != Decimal(payload.payment_amount)
        or saved.transaction_date != payload.transaction_date
        or saved.reason != payload.reason
        or receipt.response_status != 201
    ):
        raise integrity()
    expected = response_from_rows(session, saved.id)
    try:
        original = BusinessRefundResponse.model_validate(receipt.response)
    except ValidationError:
        raise integrity() from None
    if (
        original != expected
        or receipt.operation_id != original.operation_id
        or original.allocation_return_ids != tuple(payload.allocation_return_ids)
        or len(original.fees) != len(payload.fees)
    ):
        raise integrity()
    for fee, requested in zip(original.fees, payload.fees, strict=True):
        if (
            fee.account_id != requested.account_id
            or fee.asset_id != requested.asset_id
            or fee.category_id != requested.category_id
            or Decimal(fee.amount) != Decimal(requested.amount)
        ):
            raise integrity()
    return original
