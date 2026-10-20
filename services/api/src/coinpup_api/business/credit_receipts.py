"""Semantic receipt quantities use the complete stored asset, including on replay."""

from decimal import Decimal

from pydantic import ValidationError
from sqlalchemy import func, select

from coinpup_api.business.credit_models import BusinessCredit
from coinpup_api.business.credit_schemas import BusinessCreditResponse
from coinpup_api.ledger.assets import AssetDefinition
from coinpup_api.ledger.common import asset_definition
from coinpup_api.ledger.errors import MoneyError
from coinpup_api.ledger.idempotency import receipt_hash_version
from coinpup_api.ledger.models import AssetRecord
from coinpup_api.ledger.money import Amount
from coinpup_api.ledger.service import LedgerError


def validate_credit_receipt_amounts(response: BusinessCreditResponse, asset: AssetDefinition):
    """Validate exact storage padding without rewriting any original receipt string."""
    if not isinstance(asset, AssetDefinition) or asset.asset_id != response.asset_id:
        raise MoneyError("credit_integrity", "The credit receipt asset is inconsistent.")
    for item in response.postings:
        net, tax, total = (
            Amount.from_decimal(Decimal(value), asset)
            for value in (item.net_amount, item.tax_amount, item.total_amount)
        )
        if (
            net.minor_units < 0
            or tax.minor_units < 0
            or total.minor_units <= 0
            or net + tax != total
        ):
            raise MoneyError("credit_integrity", "The credit receipt quantities are inconsistent.")
    return response


def integrity():
    return LedgerError("credit_integrity", 503, "The stored credit is inconsistent.")


def receipt_asset(session, identifier):
    row = session.get(AssetRecord, identifier)
    if row is None:
        raise integrity()
    return asset_definition(row)


def response_from_rows(session, identifier):
    try:
        result = BusinessCreditResponse.model_validate(
            session.scalar(select(func.coinpup_business_credit_response(identifier)))
        )
        return validate_credit_receipt_amounts(result, receipt_asset(session, result.asset_id))
    except (ValidationError, MoneyError):
        raise integrity() from None


def replay_credit(session, receipt, ledger_id, payload, digest):
    if receipt_hash_version(receipt) != 2 or receipt.request_hash != digest:
        raise LedgerError("idempotency_conflict", 409, "This key belongs to a different request.")
    saved = session.scalar(
        select(BusinessCredit).where(
            BusinessCredit.id == payload.id,
            BusinessCredit.ledger_id == ledger_id,
            BusinessCredit.idempotency_key == receipt.key,
        )
    )
    if (
        saved is None
        or saved.document_id != payload.document_id
        or saved.source_version != payload.expected_version
        or saved.credit_date != payload.credit_date
        or saved.reason != payload.reason
        or receipt.response_status != 201
    ):
        raise integrity()
    expected = response_from_rows(session, saved.id)
    try:
        original = BusinessCreditResponse.model_validate(receipt.response)
        asset = receipt_asset(session, original.asset_id)
        validate_credit_receipt_amounts(original, asset)
        if (
            original != expected
            or receipt.operation_id != original.postings[0].operation_id
            or len(original.postings) != len(payload.lines)
        ):
            raise integrity()
        # The matching v2 hash already fixes the original spelling. Decode historical
        # quantities without applying today's asset availability to the old request.
        for posting, requested in zip(original.postings, payload.lines, strict=True):
            if (
                posting.source_line_id != requested.source_line_id
                or posting.recognition_date != requested.recognition_date
                or Amount.from_decimal(Decimal(posting.net_amount), asset)
                != Amount.from_decimal(Decimal(requested.net_amount), asset)
                or Amount.from_decimal(Decimal(posting.tax_amount), asset)
                != Amount.from_decimal(Decimal(requested.tax_amount), asset)
            ):
                raise integrity()
    except (ValidationError, MoneyError):
        raise integrity() from None
    return original
