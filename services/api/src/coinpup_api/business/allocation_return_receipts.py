"""Permanent return receipts verified against immutable members, never current capacity."""

from decimal import Decimal

from pydantic import ValidationError
from sqlalchemy import func, select

from coinpup_api.business.allocation_return_models import (
    BusinessAllocationReturn,
    BusinessAllocationReturnLine,
)
from coinpup_api.business.allocation_return_schemas import BusinessAllocationReturnResponse
from coinpup_api.ledger.idempotency import receipt_hash_version
from coinpup_api.ledger.service import LedgerError


def integrity():
    return LedgerError("allocation_return_integrity", 503, "The stored return is inconsistent.")


def response_from_rows(session, identifier):
    # The frozen SQL response is also the deferred constraint's canonical contract.
    try:
        return BusinessAllocationReturnResponse.model_validate(
            session.scalar(select(func.coinpup_business_allocation_return_response(identifier)))
        )
    except ValidationError:
        raise integrity() from None


def replay_return(session, receipt, ledger_id, payload, digest):
    if receipt_hash_version(receipt) != 2 or receipt.request_hash != digest:
        raise LedgerError("idempotency_conflict", 409, "This key belongs to a different request.")
    group = session.scalar(
        select(BusinessAllocationReturn).where(
            BusinessAllocationReturn.id == payload.id,
            BusinessAllocationReturn.ledger_id == ledger_id,
            BusinessAllocationReturn.idempotency_key == receipt.key,
        )
    )
    if (
        group is None
        or group.payment_id != payload.payment_id
        or group.return_date != payload.return_date
        or group.reason != payload.reason
        or receipt.response_status != 201
    ):
        raise integrity()
    expected = response_from_rows(session, group.id)
    try:
        original = BusinessAllocationReturnResponse.model_validate(receipt.response)
    except ValidationError:
        raise integrity() from None
    if original != expected or receipt.operation_id != original.postings[0].operation_id:
        raise integrity()
    line = BusinessAllocationReturnLine
    members = session.scalars(
        select(line)
        .where(line.return_id == group.id, line.ledger_id == ledger_id)
        .order_by(line.line_no)
    ).all()
    if len(members) != len(payload.lines):
        raise integrity()
    for saved, requested in zip(members, payload.lines, strict=True):
        if (
            saved.source_allocation_line_id != requested.source_allocation_line_id
            or saved.source_version != requested.expected_version
            or saved.payment_amount != Decimal(requested.payment_amount)
            or saved.document_amount != Decimal(requested.document_amount)
        ):
            raise integrity()
    return original
