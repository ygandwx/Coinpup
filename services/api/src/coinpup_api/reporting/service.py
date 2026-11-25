"""Owned original series and direct exact-day display valuation in one snapshot."""

from collections import defaultdict

from coinpup_api.ledger import MoneyError, get_asset
from coinpup_api.ledger.service import LedgerError, LedgerService, _page
from coinpup_api.quotes.effective import price_choices, valuation_quote
from coinpup_api.quotes.schemas import AutomaticPriceResponse, ManualPriceResponse
from coinpup_api.reporting.asset_totals import OriginalSubtotal
from coinpup_api.reporting.schemas import ReportOriginal, ReportResponse, ReportRow, ReportValuation
from coinpup_api.reporting.scope import report_scope, require_owned_ledgers
from coinpup_api.reporting.source import balance_facts, journal_facts, linked_repayments
from coinpup_api.reporting.statement import build_statement


def _side_totals(statement, facts, view):
    inflows, outflows = defaultdict(int), defaultdict(int)
    for detail in statement.rows:
        if detail.excluded_repayment_group is not None:
            continue
        original = detail.source.original
        units = original.minor_units
        # Expense reversals reduce expense in the operating view. They are not income.
        inflow = (
            facts[detail.source.reference].role == "income" if view == "operating" else units > 0
        )
        if inflow:
            inflows[original.asset] += units
        else:
            outflows[original.asset] -= units
    return tuple(
        ReportOriginal(
            asset_id=total.asset.asset_id,
            amount=total.quantity,
            inflow=None
            if view == "balances"
            else OriginalSubtotal(total.asset, inflows[total.asset]).quantity,
            outflow=None
            if view == "balances"
            else OriginalSubtotal(total.asset, outflows[total.asset]).quantity,
        )
        for total in statement.original_totals
    )


def _prices(session, owner, facts, price_date, display, price_mode):
    if display is None:
        return None, {}
    catalog = {fact.row.original.asset.asset_id: fact.row.original.asset for fact in facts.values()}
    days = (
        {price_date}
        if price_date is not None
        else {fact.transaction_date for fact in facts.values()}
    )
    choices = price_choices(
        session, owner, catalog, display.asset_id, dates=None if price_mode == "latest" else days
    )
    prices, evidence = {}, {}
    for ref, fact in facts.items():
        asset = ref.asset.asset_id
        key = asset if price_mode == "latest" else (asset, price_date or fact.transaction_date)
        price = choices.get(key)
        if price is not None:
            evidence[ref] = price
            prices[ref] = valuation_quote(ref.asset, display, price)
    return prices, evidence


def _row(detail, fact, memberships, display, evidence):
    original = detail.source.original
    price = evidence[fact.row.reference] if detail.price is not None else None
    if detail.excluded_repayment_group is not None:
        state = "excluded"
    elif display is None:
        state = "not_requested"
    elif original.asset == display:
        state = "identity"
    else:
        state = "quoted" if price is not None else "missing"
    return ReportRow(
        ledger_id=detail.source.ledger_id,
        source_id=detail.source.source_id,
        asset_id=original.asset.asset_id,
        amount=original.to_string(),
        date=fact.date,
        operation_id=fact.operation_id,
        account_id=fact.account_id,
        category_id=fact.category_id,
        description=fact.description,
        repayment_group_id=memberships.get(detail.source.reference),
        excluded_repayment_group=detail.excluded_repayment_group,
        valuation_state=state,
        price=price if isinstance(price, ManualPriceResponse) else None,
        **({"automatic_price": price} if isinstance(price, AutomaticPriceResponse) else {}),
    )


class MvpReportService(LedgerService):
    def read(
        self,
        owner,
        ledgers,
        view,
        *,
        from_date=None,
        to_date=None,
        display_asset_id=None,
        price_date=None,
        price_mode=None,
        limit=100,
        offset=0,
    ):
        _page(limit, offset)
        scope = report_scope(ledgers)
        if from_date is not None and (
            view == "balances" or (to_date is not None and from_date > to_date)
        ):
            raise LedgerError(
                "invalid_report_period", 422, "Choose an ordered range for this view."
            )
        if (
            (display_asset_id is None) != (price_date is None and price_mode is None)
            or (price_date is not None and price_mode is not None)
            or (price_mode == "historical" and view == "balances" and to_date is None)
        ):
            raise LedgerError(
                "invalid_report_valuation", 422, "Choose a display currency and one price basis."
            )
        display = get_asset(display_asset_id) if display_asset_id else None
        with self._transaction(owner, read_only=True) as session:
            require_owned_ledgers(session, owner, scope)
            try:
                history = (
                    balance_facts(session, scope, to_date)
                    if view == "balances"
                    else journal_facts(session, scope, view, from_date, to_date)
                )
                facts = {fact.row.reference: fact for fact in history}
                links, memberships = (
                    linked_repayments(session, owner, scope, history)
                    if view == "cash"
                    else ((), {})
                )
                prices, evidence = _prices(session, owner, facts, price_date, display, price_mode)
                statement = build_statement(
                    scope,
                    (fact.row for fact in history),
                    display=display,
                    selected_prices=prices,
                    cash_repayments=links,
                )
            except (MoneyError, ValueError):
                raise LedgerError(
                    "ledger_integrity", 503, "Stored report sources or quantities are inconsistent."
                ) from None

            rows = tuple(
                _row(detail, facts[detail.source.reference], memberships, display, evidence)
                for detail in statement.rows[offset : offset + limit]
            )
            valuation = statement.valuation
            return ReportResponse(
                view=view,
                rows=rows,
                originals=_side_totals(statement, facts, view),
                valuation=ReportValuation(
                    display_asset_id=display_asset_id,
                    price_date=price_date,
                    amount=valuation.display_quantity,
                    missing_asset_ids=tuple(
                        sorted(asset.asset_id for asset in valuation.display.missing_assets)
                    ),
                    missing_rows=valuation.missing_rows,
                )
                if valuation is not None
                else None,
                total_rows=len(statement.rows),
                limit=limit,
                offset=offset,
            )
