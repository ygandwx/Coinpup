"""Semantic receipt quantities use the complete stored asset, including on replay."""

from decimal import Decimal

from coinpup_api.business.credit_schemas import BusinessCreditResponse
from coinpup_api.ledger.assets import AssetDefinition
from coinpup_api.ledger.errors import MoneyError
from coinpup_api.ledger.money import Amount


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
