"""Read-only direct valuations. No ledger writes, provider selection, clock reads or I/O."""

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from fractions import Fraction

from coinpup_api.ledger import Amount, AssetDefinition, get_asset

_DISPLAY = tuple(get_asset(code) for code in ("USD", "GBP", "EUR", "HKD", "CNY"))
_PRICE = re.compile(r"(?:0|[1-9][0-9]{0,19})(?:\.[0-9]{1,18})?")


@dataclass(frozen=True, slots=True)
class DirectQuote:
    """One unit of the entire base asset in major units of the display currency."""

    base: AssetDefinition
    display: AssetDefinition
    price: str
    source: str
    effective_at: datetime
    fetched_at: datetime

    def __post_init__(self):
        if (
            not isinstance(self.base, AssetDefinition)
            or self.display not in _DISPLAY
            or self.base == self.display
        ):
            raise ValueError("Expected distinct complete base and display assets.")
        if (
            type(self.price) is not str
            or len(self.price) > 39
            or not _PRICE.fullmatch(self.price)
            or Fraction(self.price) <= 0
        ):
            raise ValueError("Expected a positive, bounded decimal quote.")
        if (
            type(self.source) is not str
            or not 1 <= len(self.source) <= 128
            or self.source.strip() != self.source
            or not self.source.isprintable()
        ):
            raise ValueError("Expected an explicit quote source.")
        for field in ("effective_at", "fetched_at"):
            stamp = getattr(self, field)
            if not isinstance(stamp, datetime) or stamp.utcoffset() is None:
                raise ValueError("Quote timestamps must include their timezone.")
            object.__setattr__(self, field, stamp.astimezone(UTC))


@dataclass(frozen=True, slots=True)
class Valuation:
    original: Amount
    display: AssetDefinition
    quote: DirectQuote | None = None

    def __post_init__(self):
        if not isinstance(self.original, Amount) or self.display not in _DISPLAY:
            raise ValueError("Expected an original amount and a supported display currency.")
        if self.quote is not None and (
            not isinstance(self.quote, DirectQuote)
            or self.quote.base != self.original.asset
            or self.quote.display != self.display
        ):
            raise ValueError("The quote must match the exact source and target assets.")

    @property
    def exact_minor(self) -> Fraction | None:
        if self.original.asset == self.display:
            return Fraction(self.original.minor_units)
        if self.quote is None:
            return None
        return Fraction(
            self.original.minor_units * 10**self.display.scale, 10**self.original.asset.scale
        ) * Fraction(self.quote.price)


@dataclass(frozen=True, slots=True)
class ValuationTotal:
    display: AssetDefinition
    exact_known_minor: Fraction
    missing_assets: frozenset[AssetDefinition]

    @property
    def rounded_minor(self) -> int | None:
        """A complete report total only; never round individual lines first."""
        if self.missing_assets:
            return None
        value = self.exact_known_minor
        whole, remainder = divmod(abs(value.numerator), value.denominator)
        rounded = whole + (2 * remainder >= value.denominator)
        return -rounded if value < 0 else rounded


def total_valuations(display: AssetDefinition, lines) -> ValuationTotal:
    if display not in _DISPLAY:
        raise ValueError("Unsupported display currency.")
    total, missing = Fraction(0), set()
    for line in lines:
        if not isinstance(line, Valuation) or line.display != display:
            raise ValueError("Every line must use the same display currency.")
        units = line.exact_minor
        if units is None:
            missing.add(line.original.asset)
        else:
            total += units
    return ValuationTotal(display, total, frozenset(missing))
