"""ADR 0026: bounded, side-effect-free tax-exclusive pricing in integer minor units."""

import re
from dataclasses import dataclass

from coinpup_api.ledger.assets import AssetDefinition
from coinpup_api.ledger.errors import MoneyError
from coinpup_api.ledger.money import Amount

MAX_DOCUMENT_LINES = 200
_RATIO = re.compile(r"(?:0|[1-9][0-9]{0,19})(?:\.[0-9]{1,18})?")


@dataclass(frozen=True, slots=True)
class PriceInput:
    quantity: str
    unit_price: str
    discount_amount: str = "0"
    tax_rate_percent: str = "0"


@dataclass(frozen=True, slots=True)
class LinePrice:
    net: Amount
    tax: Amount
    total: Amount


@dataclass(frozen=True, slots=True)
class DocumentPrice:
    lines: tuple[LinePrice, ...]
    net: Amount
    tax: Amount
    total: Amount


def _ratio(value, code, *, positive=False):
    # Bound strings before regex and integer conversion, including malicious input.
    if not isinstance(value, str) or len(value) > 39 or _RATIO.fullmatch(value) is None:
        raise MoneyError(code, "Use a bounded non-negative ASCII decimal string.")
    integer, _, fraction = value.partition(".")
    numerator = int(integer + fraction)
    if positive and numerator == 0:
        raise MoneyError(code, "Quantity must be positive.")
    return numerator, 10 ** len(fraction)


def _round_half_up(numerator, denominator):
    units, remainder = divmod(numerator, denominator)
    return units + (2 * remainder >= denominator)


def price_line(value: PriceInput, asset: AssetDefinition) -> LinePrice:
    """Round discounted net first, then tax on that net; retain exact typed amounts."""
    if not isinstance(value, PriceInput):
        raise MoneyError("pricing_line", "A price input is required.")
    quantity, quantity_scale = _ratio(value.quantity, "pricing_quantity", positive=True)
    rate, rate_scale = _ratio(value.tax_rate_percent, "pricing_tax_rate")
    price = Amount.parse(value.unit_price, asset)
    discount = Amount.parse(value.discount_amount, asset)
    if price.minor_units < 0:
        raise MoneyError("pricing_price", "Unit price must be non-negative.")
    if discount.minor_units < 0:
        raise MoneyError("pricing_discount", "Discount must be non-negative.")
    discounted = quantity * price.minor_units - discount.minor_units * quantity_scale
    if discounted < 0:
        raise MoneyError("pricing_discount", "Discount exceeds the exact line value.")
    net = Amount(asset, _round_half_up(discounted, quantity_scale))
    tax = Amount(asset, _round_half_up(net.minor_units * rate, rate_scale * 100))
    return LinePrice(net=net, tax=tax, total=net + tax)


def price_document(values: list[PriceInput] | tuple[PriceInput, ...], asset) -> DocumentPrice:
    """Sum rounded lines without whole-document repricing; drafts may be empty or zero."""
    if not isinstance(values, (list, tuple)) or len(values) > MAX_DOCUMENT_LINES:
        raise MoneyError("pricing_lines", "A document must contain at most 200 price inputs.")
    # Validate identity/availability even for the empty draft.
    zero = Amount.parse("0", asset)
    lines = tuple(price_line(value, asset) for value in values)
    net = tax = total = zero
    for line in lines:
        net, tax, total = net + line.net, tax + line.tax, total + line.total
    return DocumentPrice(lines=lines, net=net, tax=tax, total=total)
