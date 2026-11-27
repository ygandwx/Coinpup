"""Build a read-only report from one already authorized database snapshot."""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date
from uuid import UUID

from coinpup_api.ledger import Amount, AssetDefinition
from coinpup_api.ledger.assets import custodial_family
from coinpup_api.ledger.money import AggregateAmount
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
    original: Amount | AggregateAmount

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
class CashPlatformLink:
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
    price: ManualPrice | DirectQuote | None
    excluded_repayment_group: UUID | None
    excluded_platform_transfer: UUID | None = None

    @property
    def excluded(self):
        return (
            self.excluded_repayment_group is not None or self.excluded_platform_transfer is not None
        )


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


def _internal_platform_transfers(rows, links):
    exclusions = {}
    for link in links:
        if link.outgoing not in rows or link.incoming not in rows:
            continue
        outgoing, incoming = rows[link.outgoing].original, rows[link.incoming].original
        family = custodial_family(outgoing.asset)
        if (
            family is None
            or family != custodial_family(incoming.asset)
            or (outgoing.asset.kind == "custodial") == (incoming.asset.kind == "custodial")
            or outgoing.minor_units >= 0
            or outgoing.minor_units * 10**incoming.asset.scale
            + incoming.minor_units * 10**outgoing.asset.scale
            != 0
            or link.outgoing in exclusions
            or link.incoming in exclusions
        ):
            raise ValueError("Inconsistent internal platform transfer in report.")
        exclusions[link.outgoing] = link.group_id
        exclusions[link.incoming] = link.group_id
    return exclusions


def build_statement(
    selected_ledgers: Iterable[UUID],
    sources: Iterable[SourceRow],
    *,
    valuation_day: date | None = None,
    display: AssetDefinition | None = None,
    manual_prices: Iterable[ManualPrice] = (),
    cash_repayments: Iterable[CashRepaymentLink] = (),
    platform_transfers: Iterable[CashPlatformLink] = (),
    selected_prices: Mapping[SourceReference, DirectQuote] | None = None,
) -> Statement:
    """Only explicit links can eliminate cash principal. Original facts remain visible."""
    if selected_prices is not None:
        if display is None or valuation_day is not None or manual_prices:
            raise ValueError("Selected prices need their display currency and one price basis.")
    elif (valuation_day is None) != (display is None) or (
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
    platform_exclusions = _internal_platform_transfers(by_reference, platform_transfers)
    if exclusions.keys() & platform_exclusions.keys():
        raise ValueError("A cash principal belongs to multiple internal groups.")
    prices: dict[AssetDefinition, ManualPrice] = {}
    for price in manual_prices:
        if price.day == valuation_day and price.quote.display == display:
            if price.quote.base in prices:
                raise ValueError("Manual price selection is ambiguous for that date.")
            prices[price.quote.base] = price

    def selection(ref):
        return prices.get(ref.asset) if selected_prices is None else selected_prices.get(ref)

    rows = tuple(
        StatementRow(
            row,
            None if ref in exclusions or ref in platform_exclusions else selection(ref),
            exclusions.get(ref),
            platform_exclusions.get(ref),
        )
        for ref, row in by_reference.items()
    )
    included = (row for row in rows if not row.excluded)
    if display is None:
        return Statement(rows, original_subtotals(row.source.original for row in included), None)
    valuation = valued_subtotals(
        display,
        (
            Valuation(
                row.source.original,
                display,
                row.price.quote if isinstance(row.price, ManualPrice) else row.price,
            )
            for row in included
        ),
    )
    return Statement(rows, valuation.originals, valuation)
