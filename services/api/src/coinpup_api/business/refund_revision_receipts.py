"""Build and replay a fixed refund revision in a caller-authorized ledger transaction."""

from decimal import Decimal

from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from coinpup_api.business.refund_revision_models import BusinessRefundRevision
from coinpup_api.business.refund_revision_schemas import (
    RefundCancellationCreate,
    RefundCorrectionCreate,
    RefundRevisionResponse,
    validate_refund_revision_amounts,
)
from coinpup_api.ledger.common import asset_definition
from coinpup_api.ledger.errors import MoneyError
from coinpup_api.ledger.idempotency import receipt_hash_version
from coinpup_api.ledger.models import AssetRecord
from coinpup_api.ledger.money import Amount
from coinpup_api.ledger.service import LedgerError


def integrity():
    return LedgerError(
        "refund_revision_integrity", 503, "The stored refund revision is inconsistent."
    )


def _stored_response(session, identifier):
    try:
        response = RefundRevisionResponse.model_validate(
            session.scalar(select(func.coinpup_business_refund_revision_response(identifier)))
        )
        if response.revision_id != identifier:
            raise integrity()
        identifiers = set()
        for fact in (response.previous, response.replacement):
            if fact is not None:
                identifiers.update((fact.asset_id, fact.payment_asset_id))
                identifiers.update(fee.asset_id for fee in fact.fees)
        records = session.scalars(select(AssetRecord).where(AssetRecord.asset_id.in_(identifiers)))
        assets = {record.asset_id: asset_definition(record) for record in records}
        return validate_refund_revision_amounts(response, assets), assets
    except (ValidationError, MoneyError):
        raise integrity() from None
    except IntegrityError as error:
        if getattr(error.orig, "sqlstate", None) == "23514" and getattr(
            getattr(error.orig, "diag", None), "constraint_name", None
        ) in {"ck_refund_revision_shape", "ck_refund_posting_fact"}:
            raise integrity() from None
        raise


def response_from_rows(session, identifier):
    return _stored_response(session, identifier)[0]


def validate_revision_intent(response, action, payload, assets):
    command = {"correct": RefundCorrectionCreate, "cancel": RefundCancellationCreate}.get(action)
    if (
        command is None
        or not isinstance(payload, command)
        or response.action != action
        or response.previous_version != payload.expected_version
        or response.reason != payload.reason
    ):
        raise integrity()
    try:
        validate_refund_revision_amounts(response, assets)
        if action == "cancel":
            return response
        fact, requested = response.replacement, payload.replacement
        if (
            fact.account_id != requested.account_id
            or fact.asset_id != requested.asset_id
            or fact.transaction_date != requested.transaction_date
            or fact.reason != requested.reason
            or fact.allocation_return_ids != tuple(requested.allocation_return_ids)
            or len(fact.fees) != len(requested.fees)
        ):
            raise integrity()

        def matches(stored, submitted, asset_id):
            # Both values belong to an already accepted command; its assets can be disabled now.
            return Amount.from_decimal(Decimal(stored), assets[asset_id]) == Amount.from_decimal(
                Decimal(submitted), assets[asset_id]
            )

        if not matches(fact.amount, requested.amount, fact.asset_id) or not matches(
            fact.payment_amount, requested.payment_amount, fact.payment_asset_id
        ):
            raise integrity()
        for actual, wanted in zip(fact.fees, requested.fees, strict=True):
            if (
                actual.account_id != wanted.account_id
                or actual.asset_id != wanted.asset_id
                or actual.category_id != wanted.category_id
                or not matches(actual.amount, wanted.amount, actual.asset_id)
            ):
                raise integrity()
        return response
    except MoneyError:
        raise integrity() from None


def replay_revision(session, receipt, ledger_id, refund_id, action, payload, digest):
    """Only the matching v2 key, original scope and complete immutable 200 prove success."""
    if receipt_hash_version(receipt) != 2 or receipt.request_hash != digest:
        raise LedgerError("idempotency_conflict", 409, "This key belongs to a different request.")
    if receipt.ledger_id != ledger_id or receipt.response_status != 200:
        raise integrity()
    saved = session.scalar(
        select(BusinessRefundRevision).where(
            BusinessRefundRevision.ledger_id == ledger_id,
            BusinessRefundRevision.refund_id == refund_id,
            BusinessRefundRevision.idempotency_key == receipt.key,
        )
    )
    if (
        saved is None
        or saved.action != action
        or saved.operation_version != payload.expected_version + 1
        or saved.revision_reason != payload.reason
        or receipt.operation_id != saved.operation_id
    ):
        raise integrity()
    expected, assets = _stored_response(session, saved.id)
    try:
        original = RefundRevisionResponse.model_validate(receipt.response)
    except ValidationError:
        raise integrity() from None
    if (
        receipt.response != expected.model_dump(mode="json")
        or original.ledger_id != ledger_id
        or original.refund_id != refund_id
        or original.operation_id != saved.operation_id
        or original.payment_id != saved.payment_id
    ):
        raise integrity()
    return validate_revision_intent(original, action, payload, assets)
