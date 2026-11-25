"""Fictional B-12 payment attribution without repeating a bank movement or another project."""

from uuid import uuid4

import pytest
from coinpup_api.business.schemas import PartyCreate, ProjectCreate
from coinpup_api.business.settlement_schemas import BusinessPaymentCreate
from coinpup_api.ledger.schemas import AccountCreate, CategoryCreate, EntityCreate

from tests.integration.business.test_allocation_return_service import setup_return, submit_return
from tests.integration.business.test_confirmation_service import prepare, submit
from tests.integration.business.test_draft_service import drafts as drafts
from tests.integration.business.test_project_activity import page
from tests.integration.business.test_return_revision_service import cancel, command, run
from tests.integration.business.test_settlement_service import pay
from tests.integration.ledger.test_posting_service_database import ledger_setup as ledger_setup
from tests.integration.ledger.test_posting_service_database import opening

pytestmark = pytest.mark.integration


def make_company(s, country):
    ent = s["structure"].create_entity(
        s["owner"],
        EntityCreate(
            kind="company",
            name="Fictional " + country,
            country_code=country,
            company_type="private_limited" if country == "HK" else "limited_liability",
            base_asset_id="USD",
        ),
    )
    ledger = ent.ledger.id
    project = s["master"].create_project(
        s["owner"], ledger, ProjectCreate(id=uuid4(), name="Fictional project X")
    )
    party = s["master"].create_party(
        s["owner"], ledger, PartyCreate(id=uuid4(), name="Fictional trading partner", role="both")
    )
    account = s["structure"].create_account(
        s["owner"], ledger, AccountCreate(name="Fictional bank", kind="bank", asset_ids=["USD"])
    )
    income, expense = (
        s["structure"].create_category(
            s["owner"], ledger, CategoryCreate(name="Fictional " + k, kind=k)
        )
        for k in ("income", "expense")
    )
    return s | dict(
        entity=ent,
        ledger=ledger,
        project=project,
        party=party,
        account=account,
        income=income,
        expenses=[expense],
    )


def test_two_company_project_x_separates_cash_shares_from_income_cost_and_open_items(drafts):
    s = make_company(drafts, "CN")
    other = make_company(drafts, "HK")
    s["posting"].post_opening(s["owner"], s["ledger"], opening(s, "1000"), "fictional-project-open")
    for kind, price, applied in [
        ("invoice", "1000", "600"),
        ("bill", "300", "300"),
        ("bill", "100", None),
    ]:
        doc, req = prepare(
            s,
            kind=kind,
            changes={"issue_date": "2026-09-30", "party_id": s["party"].id},
            line_changes={
                "unit_price": price,
                "project_id": s["project"].id,
                "tax_rate_percent": "0",
            },
        )
        submit(s, doc, req, str(uuid4()))
        if applied:
            body = BusinessPaymentCreate(
                id=uuid4(),
                direction="receipt" if kind == "invoice" else "payout",
                party_id=s["party"].id,
                account_id=s["account"].id,
                asset_id="USD",
                amount="750" if kind == "invoice" else applied,
                transaction_date="2026-09-30",
                description="Fictional project principal",
                initial_allocation=dict(
                    id=uuid4(),
                    allocation_date="2026-09-30",
                    lines=[
                        dict(
                            document_id=doc.id,
                            document_line_id=doc.lines[0].id,
                            payment_amount=applied,
                            document_amount=applied,
                        )
                    ],
                ),
            )
            pay(s, body, str(uuid4()))
    (row,) = page(s).items
    assert row.model_dump() == dict(
        asset_id="USD",
        income="1000.00",
        cost="400.00",
        net="600.00",
        closing_receivable="400.00",
        closing_payable="100.00",
        attributed_received="600.00",
        attributed_paid="300.00",
    )
    assert page(other).items == ()
    # 150 left unapplied from a receipt of 750; no project should claim it.
    (carried,) = page(s, start="2026-10-01").items
    assert (
        carried.closing_receivable,
        carried.closing_payable,
        carried.attributed_received,
        carried.attributed_paid,
    ) == ("400.00", "100.00", "0.00", "0.00")


@pytest.mark.parametrize("direction", ["payout", "receipt"])
def test_fx_return_revisions_restore_amount_in_original_payment_currency(drafts, direction):
    s = drafts
    _, _, _, body = setup_return(s, direction=direction, asset="EUR")

    def share(end, start="2026-09-01"):
        rows = {i.asset_id: i for i in page(s, start, end).items}
        return rows.get("USD"), rows.get("EUR")

    def amount(r):
        return r.attributed_paid if direction == "payout" else r.attributed_received

    # Payment has occurred but it has no project until its explicit application.
    assert share("2026-09-15") == (None, None)
    usd, eur = share("2026-09-30")
    assert amount(usd) == "110.00" and amount(eur) == "0.00"
    returned = submit_return(s, body)
    usd, eur = share("2026-10-01")
    assert amount(usd) == "88.00"
    assert (eur.closing_payable if direction == "payout" else eur.closing_receivable) == "20.00"
    assert share("2026-10-31", "2026-10-01")[0] is None
    half = body.lines[0].model_copy(update={"payment_amount": "11", "document_amount": "10"})
    change = command(body, lines=[half], day="2026-11-08")
    run(s, returned.id, change)
    assert amount(share("2026-11-08")[0]) == "99.00"
    assert amount(share("2026-10-01")[0]) == "110.00"
    run(s, returned.id, cancel(2), "fictional-undo-project-return")
    assert amount(share("2026-11-08")[0]) == "110.00"
    only = page(s, limit=1)
    assert only.has_more and only.items[0].asset_id == "EUR"
    assert page(s, limit=1, offset=1).items[0].asset_id == "USD"
