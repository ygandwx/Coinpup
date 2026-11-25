"""Fictional journals, business dates and ledger boundaries for project activity."""

from datetime import date
from uuid import uuid4

import pytest
from coinpup_api.business.document_revision_schemas import ConfirmedDocumentCancellation
from coinpup_api.business.project_activity import ProjectActivityReads
from coinpup_api.business.schemas import ProjectCreate, ProjectUpdate
from coinpup_api.business.settlement_schemas import BusinessPaymentCreate
from coinpup_api.ledger.schemas import AssetUpdate, EntityCreate
from coinpup_api.ledger.service import LedgerError

from tests.integration.business.test_allocation_return_service import setup_return, submit_return
from tests.integration.business.test_confirmation_service import prepare, submit
from tests.integration.business.test_credit_service import credit, setup_credit
from tests.integration.business.test_document_revision_commands import correction, send
from tests.integration.business.test_document_revision_journals import confirmed
from tests.integration.business.test_draft_service import drafts as drafts
from tests.integration.business.test_payment_activation import balance
from tests.integration.business.test_settlement_service import pay
from tests.integration.ledger.test_account_classes import snapshot
from tests.integration.ledger.test_posting_service_database import ledger_setup as ledger_setup
from tests.integration.ledger.test_posting_service_database import opening

pytestmark = pytest.mark.integration


def page(s, start="2026-09-01", end="2026-10-31", project=None, **kw):
    return ProjectActivityReads(s["engine"]).page(
        s["owner"],
        s["ledger"],
        project or s["project"].id,
        start=date.fromisoformat(start),
        end=date.fromisoformat(end),
        **kw,
    )


def totals(row):
    return (
        row.asset_id,
        row.income,
        row.cost,
        row.net,
        row.closing_receivable,
        row.closing_payable,
    )


@pytest.mark.parametrize("kind", ["invoice", "bill"])
def test_credit_uses_recognition_date_and_carries_prior_unpaid(drafts, kind):
    s = drafts
    _, _, _, cmd = setup_credit(s, kind)
    credit(s, cmd)
    prior = page(s, "2026-10-01", "2026-10-19").items
    month = page(s, "2026-10-20", "2026-10-20").items
    assert len(prior) == len(month) == 1
    if kind == "invoice":
        assert totals(prior[0]) == ("USD", "0.00", "0.00", "0.00", "110.00", "0.00")
        assert totals(month[0]) == ("USD", "-20.00", "0.00", "-20.00", "90.00", "0.00")
    else:
        assert totals(prior[0]) == ("USD", "0.00", "0.00", "0.00", "0.00", "110.00")
        assert totals(month[0]) == ("USD", "0.00", "-20.00", "20.00", "0.00", "90.00")


@pytest.mark.parametrize("direction", ["receipt", "payout"])
def test_fx_allocation_and_return_never_duplicate_revenue_or_fee(drafts, direction):
    s = drafts
    _, _, _, cmd = setup_return(s, direction=direction, asset="EUR")
    assert page(s, "2026-10-01", "2026-10-01").items == ()
    submit_return(s, cmd)
    before = snapshot(s)
    full, usd = page(s).items
    assert totals(usd) == ("USD", "0.00", "0.00", "0.00", "0.00", "0.00")
    assert (usd.attributed_received, usd.attributed_paid) == (
        ("88.00", "0.00") if direction == "receipt" else ("0.00", "88.00")
    )
    (october,) = page(s, "2026-10-01", "2026-10-01").items
    if direction == "receipt":
        assert totals(full) == ("EUR", "100.00", "0.00", "100.00", "20.00", "0.00")
        assert totals(october) == ("EUR", "0.00", "0.00", "0.00", "20.00", "0.00")
    else:
        assert totals(full) == ("EUR", "0.00", "100.00", "-100.00", "0.00", "20.00")
        assert totals(october) == ("EUR", "0.00", "0.00", "0.00", "0.00", "20.00")
    assert snapshot(s) == before


def test_assets_minimum_unit_project_pagination_and_archive(drafts):
    s = drafts
    for asset, amount, project in [
        ("USD", "2.03", s["project"].id),
        ("BTC", "0.00000001", s["project"].id),
        ("USD", "40", None),
    ]:
        doc, cmd = prepare(
            s,
            changes={"asset_id": asset},
            line_changes={"unit_price": amount, "tax_rate_percent": "0", "project_id": project},
        )
        submit(s, doc, cmd, str(uuid4()))
    bill, request = prepare(
        s, kind="bill", line_changes={"unit_price": "1.01", "tax_rate_percent": "0"}
    )
    submit(s, bill, request, str(uuid4()))
    s["master"].update_project(
        s["owner"], s["ledger"], s["project"].id, ProjectUpdate(expected_version=1, archived=True)
    )
    s["structure"].update_asset(s["owner"], "BTC", AssetUpdate(expected_version=1, enabled=False))
    one, two = page(s, limit=1), page(s, limit=1, offset=1)
    assert one.has_more and not two.has_more
    assert totals(one.items[0]) == (
        "BTC",
        "0.00000001",
        "0.00000000",
        "0.00000001",
        "0.00000001",
        "0.00000000",
    )
    assert totals(two.items[0]) == ("USD", "2.03", "1.01", "1.02", "2.03", "1.01")
    assert page(s, limit=1, offset=2).items == ()


def test_project_reassignment_and_cancellation_reverse_exact_sources(drafts):
    s = drafts
    doc, _ = confirmed(s, "invoice", 1)
    other = s["master"].create_project(
        s["owner"], s["ledger"], ProjectCreate(id=uuid4(), name="Fictional other")
    )
    req = correction(doc, changes={doc.lines[0].id: {"project_id": other.id, "unit_price": "2.01"}})
    with pytest.raises(LedgerError) as failure:
        send(s, doc, req, "fictional-unreviewed")
    assert failure.value.code == "document_reference_review_required"
    req = correction(
        doc,
        changes={doc.lines[0].id: {"project_id": other.id, "unit_price": "2.01"}},
        reviewed_references=[dict(kind="project", id=other.id, version=other.version)],
    )
    revised = send(s, doc, req, "fictional-project-reassign")
    assert page(s).items == ()
    assert totals(page(s, project=other.id).items[0]) == (
        "USD",
        "2.01",
        "0.00",
        "2.01",
        "2.01",
        "0.00",
    )
    send(
        s,
        doc,
        ConfirmedDocumentCancellation(expected_version=revised.version, reason="Fictional end"),
        "fictional-project-end",
    )
    assert page(s, project=other.id).items == ()


def test_http_dates_pagination_and_no_cross_ledger_or_session(drafts, authenticated_client):
    s = drafts
    client, _, owner = authenticated_client
    assert owner == s["owner"]
    project = s["project"].id
    path = f"/api/v1/ledgers/{s['ledger']}/business-projects/{project}/activity"
    params = {"start": "2026-09-01", "end": "2026-09-30"}
    assert client.get(path, params=params).json() == dict(
        ledger_id=str(s["ledger"]),
        project_id=str(project),
        **params,
        offset=0,
        limit=50,
        has_more=False,
        items=[],
    )
    for changes in [
        {"start": "0"},
        {"start": "2026-09-31"},
        {"start": "2026-10-01"},
        {"limit": 201},
        {"offset": -1},
    ]:
        assert client.get(path, params=params | changes).status_code == 422
    assert client.get(path).status_code == 422
    second = s["structure"].create_entity(
        owner,
        EntityCreate(
            kind="personal",
            name="Fictional second ledger",
            base_asset_id="USD",
            template_key="personal_default",
        ),
    )
    other = s["master"].create_project(
        owner, second.ledger.id, ProjectCreate(id=uuid4(), name=s["project"].name)
    )
    assert (
        client.get(path.replace(str(s["ledger"]), str(second.ledger.id)), params=params).status_code
        == 404
    )
    assert client.get(path.replace(str(project), str(other.id)), params=params).status_code == 404
    for part in [s["ledger"], project]:
        assert client.get(path.replace(str(part), str(uuid4())), params=params).status_code == 404
    with pytest.raises(LedgerError) as e:
        ProjectActivityReads(s["engine"]).page(
            uuid4(), s["ledger"], project, start=date(2026, 9, 1), end=date(2026, 9, 30)
        )
    assert e.value.status == 404
    client.cookies.clear()
    assert client.get(path, params=params).status_code == 401


def test_b06_two_partial_payouts_change_only_cash_and_open_items(drafts):
    s = drafts
    s["posting"].post_opening(
        s["owner"], s["ledger"], opening(s, "1000"), "fictional-project-opening"
    )
    doc, cmd = prepare(
        s,
        kind="bill",
        changes={"issue_date": "2026-09-30"},
        line_changes={"unit_price": "300", "tax_rate_percent": "0"},
    )
    submit(s, doc, cmd)
    for day, amount in [("2026-09-30", "100"), ("2026-10-01", "50")]:
        body = BusinessPaymentCreate(
            id=uuid4(),
            direction="payout",
            party_id=s["party"].id,
            account_id=s["account"].id,
            asset_id="USD",
            amount=amount,
            transaction_date=day,
            description="Fictional two installments",
            initial_allocation=dict(
                id=uuid4(),
                allocation_date=day,
                lines=[
                    dict(
                        document_id=doc.id,
                        document_line_id=doc.lines[0].id,
                        payment_amount=amount,
                        document_amount=amount,
                    )
                ],
            ),
        )
        pay(s, body, "fictional-project-" + day)
    assert totals(page(s, end="2026-09-30").items[0]) == (
        "USD",
        "0.00",
        "300.00",
        "-300.00",
        "0.00",
        "200.00",
    )
    assert totals(page(s, start="2026-10-01").items[0]) == (
        "USD",
        "0.00",
        "0.00",
        "0.00",
        "0.00",
        "150.00",
    )
    with s["engine"].connect() as c:
        assert balance(c, s["account"].id, "USD") == 850
