"""Accumulate already scoped report lines. Every original quantity keeps its asset unit."""

from collections.abc import Iterable
from dataclasses import dataclass

from coinpup_api.ledger import Amount, AssetDefinition
from coinpup_api.quotes.valuation import Valuation, ValuationTotal, total_valuations


def _key(asset):
    return asset.asset_id, asset.kind, asset.scale


def _decimal_units(units, scale):
    whole, fraction = divmod(abs(units), 10**scale)
    decimal = f".{fraction:0{scale}d}" if scale else ""
    return f"{'-' if units < 0 else ''}{whole}{decimal}"


@dataclass(frozen=True, slots=True)
class OriginalSubtotal:
    asset: AssetDefinition
    minor: int

    @property
    def quantity(self) -> str:
        # A sum can exceed one NUMERIC(38,18) posting. Never insert or clamp report totals.
        return _decimal_units(self.minor, self.asset.scale)


def _groups(values):
    return tuple(OriginalSubtotal(asset, values[asset]) for asset in sorted(values, key=_key))


def original_subtotals(amounts: Iterable[Amount]) -> tuple[OriginalSubtotal, ...]:
    values = {}
    for amount in amounts:
        if not isinstance(amount, Amount):
            raise ValueError("Expected an original asset amount.")
        values[amount.asset] = values.get(amount.asset, 0) + amount.minor_units
    return _groups(values)


@dataclass(frozen=True, slots=True)
class ValuedSubtotals:
    originals: tuple[OriginalSubtotal, ...]
    display: ValuationTotal
    missing_rows: int

    @property
    def display_quantity(self) -> str | None:
        minor = self.display.rounded_minor
        return None if minor is None else _decimal_units(minor, self.display.display.scale)


def valued_subtotals(display: AssetDefinition, lines: Iterable[Valuation]) -> ValuedSubtotals:
    """Original groups never use rates. Value each row with its own chosen quote before summing."""
    values = {}
    missing = 0

    def collect():
        nonlocal missing
        for line in lines:
            if not isinstance(line, Valuation) or line.display != display:
                raise ValueError("Every report row must name the same display currency.")
            a = line.original
            values[a.asset] = values.get(a.asset, 0) + a.minor_units
            if a.asset != display and line.quote is None:
                missing += 1
            yield line

    total = total_valuations(display, collect())
    return ValuedSubtotals(_groups(values), total, missing)
