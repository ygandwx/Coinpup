"""Check confirmed line capacity using effective settlements in the document's own asset."""

from decimal import Decimal

from sqlalchemy import func, select

from coinpup_api.business.allocation_return_projections import current_return_members
from coinpup_api.business.confirmation_reads import BusinessConfirmedDocument
from coinpup_api.business.credit_projections import current_credit_lines
from coinpup_api.business.document_revision_schemas import ConfirmedDocumentReplacement
from coinpup_api.business.settlement_models import BusinessAllocation, BusinessAllocationLine
from coinpup_api.business.settlement_totals import document_totals
from coinpup_api.ledger.assets import AssetDefinition
from coinpup_api.ledger.errors import MoneyError
from coinpup_api.ledger.money import Amount
from coinpup_api.ledger.service import LedgerError


def _integrity():
    return LedgerError(
        "document_revision_integrity", 503, "Stored document facts are inconsistent."
    )


def _dependent():
    return LedgerError(
        "document_revision_dependency",
        409,
        "Review the effective allocations and credits before revising this document.",
    )


def _has_earlier_use(session, ledger_id, source_ids, day):
    """A fully returned allocation or superseded credit date cannot bind the new issue date."""
    line, group = BusinessAllocationLine, BusinessAllocation
    earlier = (
        select(line.id, line.document_amount)
        .join(group, group.id == line.allocation_id)
        .where(
            line.ledger_id == ledger_id,
            group.ledger_id == ledger_id,
            line.document_line_id.in_(source_ids),
            group.allocation_date < day,
        )
        .cte("earlier_applications")
    )
    ids = select(func.array_agg(earlier.c.id)).correlate(None).scalar_subquery()
    live = current_return_members(ledger_id, ids)
    prior_application = (
        select(earlier.c.id)
        .outerjoin(live, live.c.source_allocation_line_id == earlier.c.id)
        .group_by(earlier.c.id, earlier.c.document_amount)
        .having(earlier.c.document_amount > func.coalesce(func.sum(live.c.document_amount), 0))
        .exists()
    )
    credit = current_credit_lines(ledger_id, source_ids).c
    prior_credit = select(credit.source_line_id).where(credit.credit_date < day).exists()
    return session.scalar(select(prior_application | prior_credit))


def check_document_revision_capacity(
    session,
    ledger_id,
    current: BusinessConfirmedDocument,
    old_asset: AssetDefinition,
    replacement: ConfirmedDocumentReplacement | None,
    new_asset: AssetDefinition | None,
):
    """Call with the current sealed version after locking the ledger and both assets.

    A removed line has zero capacity. Historical credits and fully returned allocations
    retain their receipts but consume no part of the replacement.
    """
    previous = {row.id: row for row in current.lines}
    if (
        current.ledger_id != ledger_id
        or current.asset_id != old_asset.asset_id
        or len(previous) != current.line_count
        or len(previous) != len(current.lines)
        or not previous
        or any(
            row.document_id != current.id
            or row.ledger_id != ledger_id
            or row.asset_id != current.asset_id
            for row in current.lines
        )
        or (replacement is None) != (new_asset is None)
    ):
        raise _integrity()

    proposed = {}
    if replacement is not None:
        prices = replacement.price(current.document_kind, new_asset)
        proposed = {
            row.id: (row, price) for row, price in zip(replacement.lines, prices.lines, strict=True)
        }

    totals = document_totals(session, ledger_id, previous)
    if not totals.keys() <= previous.keys():
        raise _integrity()
    if (
        replacement is not None
        and replacement.issue_date > current.issue_date
        and _has_earlier_use(session, ledger_id, list(previous), replacement.issue_date)
    ):
        raise _dependent()
    for source_id, components in totals.items():
        old = previous[source_id]
        try:
            applied, net, tax = (
                Amount.from_decimal(value, old_asset).minor_units for value in components
            )
            old_net, old_tax, old_total = (
                Amount.from_decimal(Decimal(value), old_asset).minor_units
                for value in (old.net_amount, old.tax_amount, old.total_amount)
            )
        except (MoneyError, ArithmeticError):
            raise _integrity() from None
        consumed = applied + net + tax
        if (
            min(applied, net, tax) < 0
            or net > old_net
            or tax > old_tax
            or consumed > old_total
            or old_net + old_tax != old_total
        ):
            raise _integrity()
        if consumed == 0:
            continue
        if (
            source_id not in proposed
            or old_asset != new_asset
            or current.party_id != replacement.party_id
        ):
            raise _dependent()
        line, price = proposed[source_id]
        if (
            line.project_id != old.project_id
            or ((net or tax) and line.category_id != old.category_id)
            or net > price.net.minor_units
            or tax > price.tax.minor_units
            or consumed > price.total.minor_units
        ):
            raise _dependent()
