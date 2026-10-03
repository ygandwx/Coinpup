"""Exact quantities in integer minimum units; arithmetic never uses Decimal's context."""

import re
from dataclasses import dataclass
from decimal import Decimal
from functools import total_ordering

from coinpup_api.ledger.assets import AssetDefinition
from coinpup_api.ledger.errors import MoneyError

MAX_INTEGER_DIGITS = 20
MAX_FRACTION_DIGITS = 18
MAX_AMOUNT_STRING_LENGTH = MAX_INTEGER_DIGITS + MAX_FRACTION_DIGITS + 2
_DECIMAL_TEXT = re.compile(r"-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?")


def _require_asset(asset):
    if not isinstance(asset, AssetDefinition):
        raise MoneyError("asset_required", "A validated asset definition is required.")


@total_ordering
@dataclass(frozen=True, slots=True)
class Amount:
    asset: AssetDefinition
    minor_units: int

    def __post_init__(self):
        _require_asset(self.asset)
        if type(self.minor_units) is not int:
            raise MoneyError("amount_units", "Minimum units must be an integer, not bool or float.")
        if abs(self.minor_units) >= 10 ** (MAX_INTEGER_DIGITS + self.asset.scale):
            raise MoneyError("amount_range", "Amount exceeds the 20-integer-digit storage bound.")

    @classmethod
    def parse(cls, text: str, asset: AssetDefinition) -> "Amount":
        """Strict wire input. Excess fractional digits, including trailing zeroes, are rejected."""
        _require_asset(asset)
        if not asset.enabled:
            raise MoneyError("asset_disabled", "Asset is disabled for new quantity input.")
        if not isinstance(text, str):
            raise MoneyError("amount_type", "Amount input must be a decimal string.")
        if len(text) > MAX_AMOUNT_STRING_LENGTH:
            raise MoneyError("amount_range", "Amount input is longer than the storage contract.")
        if _DECIMAL_TEXT.fullmatch(text) is None:
            raise MoneyError(
                "amount_format", "Use an ASCII decimal string without exponent notation."
            )
        negative = text.startswith("-")
        integer, _, fraction = text.removeprefix("-").partition(".")
        if len(integer) > MAX_INTEGER_DIGITS:
            raise MoneyError("amount_range", "Amount exceeds the 20-integer-digit storage bound.")
        if len(fraction) > asset.scale:
            raise MoneyError(
                "amount_precision", "Amount has more fractional digits than its asset."
            )
        units = int(integer + fraction.ljust(asset.scale, "0"))
        return cls(asset, -units if negative else units)

    @classmethod
    def from_decimal(cls, value: Decimal, asset: AssetDefinition) -> "Amount":
        """Decode a database Decimal without rounding; harmless storage padding is accepted."""
        _require_asset(asset)
        if not isinstance(value, Decimal):
            raise MoneyError("amount_type", "Database quantity must be a Decimal.")
        if not value.is_finite():
            raise MoneyError("amount_finite", "Amount must be finite.")
        if value.is_zero():
            return cls(asset, 0)
        sign, digits, exponent = value.as_tuple()
        # Check the exponent before building a power or integer, even for hostile exponents.
        adjusted = len(digits) + exponent - 1
        if adjusted >= MAX_INTEGER_DIGITS:
            raise MoneyError("amount_range", "Amount exceeds the 20-integer-digit storage bound.")
        if adjusted < -asset.scale:
            raise MoneyError("amount_precision", "Amount is smaller than the asset's minimum unit.")
        shift = exponent + asset.scale
        if shift < 0:
            if any(digits[shift:]):
                raise MoneyError("amount_precision", "Database amount exceeds its asset precision.")
            digits = digits[:shift]
            shift = 0
        coefficient = 0
        for digit in digits:
            coefficient = coefficient * 10 + digit
        units = coefficient * 10**shift
        return cls(asset, -units if sign else units)

    def to_string(self) -> str:
        digits = str(abs(self.minor_units))
        scale = self.asset.scale
        if scale:
            digits = digits.zfill(scale + 1)
            digits = digits[:-scale] + "." + digits[-scale:]
        return ("-" if self.minor_units < 0 else "") + digits

    def to_decimal(self) -> Decimal:
        # Decimal construction from a validated string is exact and context-independent.
        return Decimal(self.to_string())

    def _compatible(self, other):
        if self.asset != other.asset:
            raise MoneyError(
                "asset_mismatch", "Quantities must have the same complete asset definition."
            )

    def __add__(self, other):
        if not isinstance(other, Amount):
            return NotImplemented
        self._compatible(other)
        return Amount(self.asset, self.minor_units + other.minor_units)

    def __sub__(self, other):
        if not isinstance(other, Amount):
            return NotImplemented
        self._compatible(other)
        return Amount(self.asset, self.minor_units - other.minor_units)

    def __neg__(self):
        return Amount(self.asset, -self.minor_units)

    def __lt__(self, other):
        if not isinstance(other, Amount):
            return NotImplemented
        self._compatible(other)
        return self.minor_units < other.minor_units
