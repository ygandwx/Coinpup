"""Unrouted immutable 200 adapters for an already authorized, caller-owned transaction."""

from decimal import Decimal

from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from coinpup_api.business.credit_revision_models import BusinessCreditRevision
from coinpup_api.business.credit_revision_schemas import (
    CreditCancellationCreate,
    CreditCorrectionCreate,
    CreditRevisionResponse,
    validate_credit_revision_amounts,
)
from coinpup_api.ledger.common import asset_definition
from coinpup_api.ledger.errors import MoneyError
from coinpup_api.ledger.idempotency import receipt_hash_version
from coinpup_api.ledger.models import AssetRecord
from coinpup_api.ledger.money import Amount
from coinpup_api.ledger.service import LedgerError


def integrity():
    return LedgerError(
        "credit_revision_integrity", 503, "The stored credit revision is inconsistent."
    )


def _stored_response(session, identifier):
    try:
        response = CreditRevisionResponse.model_validate(
            session.scalar(select(func.coinpup_business_credit_revision_response(identifier)))
        )
        if response.revision_id != identifier:
            raise integrity()
        record = session.get(AssetRecord, response.asset_id)
        if record is None:
            raise integrity()
        asset = asset_definition(record)
        return validate_credit_revision_amounts(response, asset), asset
    except (ValidationError, MoneyError):
        raise integrity() from None
    except IntegrityError as error:
        if (
            getattr(error.orig, "sqlstate", None) == "23514"
            and getattr(getattr(error.orig, "diag", None), "constraint_name", None)
            == "ck_credit_revision_shape"
        ):
            raise integrity() from None
        raise


def response_from_rows(session, identifier):
    """Read one fixed revision, never current membership or today's reference availability."""
    return _stored_response(session, identifier)[0]


def validate_revision_intent(response, action, payload, asset):
    """Check validated original input against frozen facts without rewriting stored strings."""
    try:
        validate_credit_revision_amounts(response, asset)
        command = {"correct": CreditCorrectionCreate, "cancel": CreditCancellationCreate}.get(
            action
        )
        if (
            command is None
            or not isinstance(payload, command)
            or response.action != action
            or response.previous_version != payload.expected_version
            or response.reason != payload.reason
        ):
            raise integrity()
        if action == "correct":
            replacement = payload.replacement
            current = [item for item in response.members if item.action != "cancel"]
            if (
                response.credit_date != replacement.credit_date
                or response.replacement_reason != replacement.reason
                or len(current) != len(replacement.lines)
            ):
                raise integrity()
            for item, requested in zip(current, replacement.lines, strict=True):
                if (
                    item.source_line_id != requested.source_line_id
                    or item.recognition_date != requested.recognition_date
                    or any(
                        Amount.from_decimal(Decimal(getattr(item, field)), asset)
                        != Amount.from_decimal(Decimal(getattr(requested, field)), asset)
                        for field in ("net_amount", "tax_amount")
                    )
                ):
                    raise integrity()
        return response
    except MoneyError:
        raise integrity() from None


def replay_revision(session, receipt, ledger_id, credit_id, action, payload, digest):
    """Caller must authenticate/lock the ledger first and compute the independent v2 digest.

    The fixed receipt wins before current version, terminal state, periods or availability.
    This helper neither owns a transaction nor creates an independently authorized endpoint.
    """
    if receipt_hash_version(receipt) != 2 or receipt.request_hash != digest:
        raise LedgerError("idempotency_conflict", 409, "This key belongs to a different request.")
    if receipt.ledger_id != ledger_id:
        raise integrity()
    saved = session.scalar(
        select(BusinessCreditRevision).where(
            BusinessCreditRevision.ledger_id == ledger_id,
            BusinessCreditRevision.credit_id == credit_id,
            BusinessCreditRevision.idempotency_key == receipt.key,
        )
    )
    if (
        saved is None
        or saved.action != action
        or saved.group_version != payload.expected_version + 1
        or saved.revision_reason != payload.reason
        or receipt.response_status != 200
    ):
        raise integrity()
    expected, asset = _stored_response(session, saved.id)
    try:
        original = CreditRevisionResponse.model_validate(receipt.response)
        if (
            receipt.response != expected.model_dump(mode="json")
            or original.ledger_id != ledger_id
            or original.credit_id != credit_id
            or receipt.operation_id != original.members[0].operation_id
        ):
            raise integrity()
        return validate_revision_intent(original, action, payload, asset)
    except ValidationError:
        raise integrity() from None
