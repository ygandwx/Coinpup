"""Rebuild a fixed return revision and replay its original scoped command."""

from decimal import Decimal

from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from coinpup_api.business.allocation_return_receipts import integrity
from coinpup_api.business.allocation_return_revision_models import (
    BusinessAllocationReturnRevision as Revision,
)
from coinpup_api.business.allocation_return_revision_schemas import (
    AllocationReturnCancellationCreate,
    AllocationReturnCorrectionCreate,
    AllocationReturnRevisionResponse,
    validate_return_revision_amounts,
)
from coinpup_api.ledger.common import asset_definition
from coinpup_api.ledger.errors import MoneyError
from coinpup_api.ledger.idempotency import receipt_hash_version
from coinpup_api.ledger.models import AssetRecord
from coinpup_api.ledger.money import Amount
from coinpup_api.ledger.service import LedgerError


def _stored_response(session, identifier):
    try:
        response = AllocationReturnRevisionResponse.model_validate(
            session.scalar(select(func.coinpup_business_return_revision_response(identifier)))
        )
        if response.revision_id != identifier:
            raise integrity()
        identifiers = {response.payment_asset_id}
        identifiers.update(member.document_asset_id for member in response.members)
        records = session.scalars(select(AssetRecord).where(AssetRecord.asset_id.in_(identifiers)))
        assets = {record.asset_id: asset_definition(record) for record in records}
        return validate_return_revision_amounts(response, assets), assets
    except (ValidationError, MoneyError):
        raise integrity() from None
    except IntegrityError as error:
        if (
            getattr(error.orig, "sqlstate", None) == "23514"
            and getattr(getattr(error.orig, "diag", None), "constraint_name", None)
            == "ck_return_revision_shape"
        ):
            raise integrity() from None
        raise


def response_from_rows(session, identifier):
    return _stored_response(session, identifier)[0]


def fixed_revision_receipt(session, saved, receipt):
    """Verify the original status, identity and complete wire body against immutable facts."""
    if (
        receipt is None
        or receipt.ledger_id != saved.ledger_id
        or receipt.key != saved.idempotency_key
        or receipt.hash_version != 2
        or receipt.response_status != 200
    ):
        raise integrity()
    expected, assets = _stored_response(session, saved.id)
    try:
        original = AllocationReturnRevisionResponse.model_validate(receipt.response)
    except ValidationError:
        raise integrity() from None
    if (
        receipt.response != expected.model_dump(mode="json")
        or original.ledger_id != saved.ledger_id
        or original.return_id != saved.return_id
        or original.payment_id != saved.payment_id
        or original.version != saved.group_version
        or original.action != saved.action
        or original.reason != saved.revision_reason
        or receipt.operation_id != original.members[0].operation_id
    ):
        raise integrity()
    return original, assets


def validate_revision_intent(response, action, payload, assets):
    command = {
        "correct": AllocationReturnCorrectionCreate,
        "cancel": AllocationReturnCancellationCreate,
    }
    if (
        action not in command
        or not isinstance(payload, command[action])
        or response.action != action
        or response.previous_version != payload.expected_version
        or response.reason != payload.reason
    ):
        raise integrity()
    if action == "cancel":
        return response
    desired = payload.replacement
    active = [member for member in response.members if member.replacement is not None]
    if (
        response.replacement.return_date != desired.return_date
        or response.replacement.reason != desired.reason
        or len(active) != len(desired.lines)
    ):
        raise integrity()

    def matches(actual, submitted, identity):
        # Both values belong to a previously accepted intent; its asset may now be disabled.
        return Amount.from_decimal(Decimal(actual), assets[identity]) == Amount.from_decimal(
            Decimal(submitted), assets[identity]
        )

    try:
        for member, line in zip(active, desired.lines, strict=True):
            if (
                member.source_allocation_line_id != line.source_allocation_line_id
                or member.source_version != line.expected_version
                or not matches(
                    member.replacement.payment_amount,
                    line.payment_amount,
                    response.payment_asset_id,
                )
                or not matches(
                    member.replacement.document_amount,
                    line.document_amount,
                    member.document_asset_id,
                )
            ):
                raise integrity()
    except MoneyError:
        raise integrity() from None
    return response


def replay_revision(session, receipt, ledger_id, return_id, action, payload, digest):
    """The caller supplies an authorized ledger and the v2 digest of the original raw JSON."""
    if receipt is None:
        raise integrity()
    if receipt_hash_version(receipt) != 2 or receipt.request_hash != digest:
        raise LedgerError("idempotency_conflict", 409, "This key belongs to a different request.")
    if receipt.ledger_id != ledger_id or receipt.response_status != 200:
        raise integrity()
    saved = session.scalar(
        select(Revision).where(
            Revision.ledger_id == ledger_id,
            Revision.return_id == return_id,
            Revision.idempotency_key == receipt.key,
        )
    )
    if saved is None:
        raise integrity()
    response, assets = fixed_revision_receipt(session, saved, receipt)
    return validate_revision_intent(response, action, payload, assets)
