"""Build a read-only report from one already authorized database snapshot."""

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from uuid import UUID

from coinpup_api.ledger import Amount, AssetDefinition
from coinpup_api.quotes.valuation import DirectQuote, Valuation
from coinpup_api.reporting.asset_totals import (
    OriginalSubtotal,
    ValuedSubtotals,
    original_subtotals,
    valued_subtotals,
)


@dataclass(frozen=True, slots=True)
class SourceReference:
    ledger_id: UUID
    source_id: UUID
    asset: AssetDefinition


@dataclass(frozen=True, slots=True)
class SourceRow:
    """The caller supplies the original source identity from the chosen report view."""

    ledger_id: UUID
    source_id: UUID
    original: Amount

    @property
    def reference(self) -> SourceReference:
        return SourceReference(self.ledger_id, self.source_id, self.original.asset)


@dataclass(frozen=True, slots=True)
class CashRepaymentLink:
    """Two exact principal postings obtained from a verified interledger repayment."""

    group_id: UUID
    outgoing: SourceReference
    incoming: SourceReference


@dataclass(frozen=True, slots=True)
class ManualPrice:
    day: date
    quote: DirectQuote

    def __post_init__(self):
        if (
            type(self.day) is not date
            or not isinstance(self.quote, DirectQuote)
            or self.quote.effective_at.date() != self.day
        ):
            raise ValueError("Manual price evidence must match its explicit UTC date.")


@dataclass(frozen=True, slots=True)
class StatementRow:
    source: SourceRow
    price: ManualPrice | None
    excluded_repayment_group: UUID | None


@dataclass(frozen=True, slots=True)
class Statement:
    rows: tuple[StatementRow, ...]
    original_totals: tuple[OriginalSubtotal, ...]
    valuation: ValuedSubtotals | None


def _internal_repayments(
    rows: dict[SourceReference, SourceRow], links: Iterable[CashRepaymentLink]
) -> dict[SourceReference, UUID]:
    excluded: dict[SourceReference, UUID] = {}
    for link in links:
        if link.outgoing not in rows or link.incoming not in rows:
            # A single selected ledger or a filtered-out counterpart retains its real cash.
            continue
        outgoing = rows[link.outgoing].original
        incoming = rows[link.incoming].original
        if (
            link.outgoing.ledger_id == link.incoming.ledger_id
            or outgoing.asset != incoming.asset
            or outgoing.minor_units >= 0
            or outgoing.minor_units + incoming.minor_units != 0
            or link.outgoing in excluded
            or link.incoming in excluded
        ):
            raise ValueError("Inconsistent internal repayment references in report.")
        excluded[link.outgoing] = link.group_id
        excluded[link.incoming] = link.group_id
    return excluded


def build_statement(
    selected_ledgers: Iterable[UUID],
    sources: Iterable[SourceRow],
    *,
    valuation_day: date | None = None,
    display: AssetDefinition | None = None,
    manual_prices: Iterable[ManualPrice] = (),
    cash_repayments: Iterable[CashRepaymentLink] = (),
) -> Statement:
    """Only explicit links can eliminate cash principal. Original facts remain visible."""
    if (valuation_day is None) != (display is None) or (
        valuation_day is not None and type(valuation_day) is not date
    ):
        raise ValueError("Supply both a valuation date and display currency, or neither.")

    scope = frozenset(selected_ledgers)
    by_reference: dict[SourceReference, SourceRow] = {}
    for row in sources:
        reference = row.reference
        if row.ledger_id not in scope or reference in by_reference:
            raise ValueError("Unexpected or duplicate original row in report.")
        by_reference[reference] = row

    exclusions = _internal_repayments(by_reference, cash_repayments)
    prices: dict[AssetDefinition, ManualPrice] = {}
    for price in manual_prices:
        if price.day == valuation_day and price.quote.display == display:
            if price.quote.base in prices:
                raise ValueError("Manual price selection is ambiguous for that date.")
            prices[price.quote.base] = price

    rows = tuple(
        StatementRow(row, None if ref in exclusions else prices.get(ref.asset), exclusions.get(ref))
        for ref, row in by_reference.items()
    )
    included = (row for row in rows if row.excluded_repayment_group is None)
    if display is None:
        return Statement(rows, original_subtotals(row.source.original for row in included), None)
    valuation = valued_subtotals(
        display,
        (
            Valuation(row.source.original, display, row.price.quote if row.price else None)
            for row in included
        ),
    )
    return Statement(rows, valuation.originals, valuation)
