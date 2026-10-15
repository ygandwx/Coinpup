"""Prepare refund cash/control legs and independent fees before any persistence."""

from uuid import uuid4

from coinpup_api.business.settlement_rows import PostingGroup
from coinpup_api.ledger.posting_storage import PostingLine, prepare_journal_lines


def refund_rows(service, session, ledger_id, party_id, payload, plan, controls, catalog):
    operation, journal = uuid4(), uuid4()
    common = dict(journal_id=journal, ledger_id=ledger_id, party_id=party_id)
    rows = [
        PostingLine(
            line_no=1,
            role="account",
            account_id=payload.account_id,
            asset_id=plan.cash_amount.asset.asset_id,
            amount=plan.cash_amount,
            **common,
        ),
        PostingLine(
            line_no=2,
            role="account",
            account_id=controls[plan.control_key].id,
            asset_id=plan.control_amount.asset.asset_id,
            amount=plan.control_amount,
            **common,
        ),
    ]
    if plan.cross_asset:
        # Actual cash and original principal are explicit independent asset quantities.
        # Clearing each asset does not recognize an expense or infer a conversion rate.
        rows.extend(
            PostingLine(
                line_no=position,
                role="exchange",
                asset_id=quantity.asset.asset_id,
                amount=-quantity,
                **common,
            )
            for position, quantity in enumerate((plan.cash_amount, plan.control_amount), 3)
        )
    service._append_fees(session, ledger_id, rows, payload.fees, catalog)
    return PostingGroup(
        operation,
        journal,
        payload.reason,
        tuple(rows),
        tuple(prepare_journal_lines(rows, catalog)),
    )
