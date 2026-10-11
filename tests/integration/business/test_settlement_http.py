"""Fictional HTTP settlement lifecycle, precise reads and original-intent replay."""

import json
from uuid import uuid4

import pytest
from coinpup_api.ledger.schemas import EntityCreate

from tests.integration.business.test_draft_service import drafts as drafts
from tests.integration.business.test_settlement_service import prepared
from tests.integration.ledger.test_account_classes import snapshot
from tests.integration.ledger.test_period_transactions import change as change_period
from tests.integration.ledger.test_posting_service_database import ledger_setup as ledger_setup

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("direction", ["receipt", "payout"])
@pytest.mark.parametrize("asset", ["USD", "EUR"])
def test_atomic_initial_and_later_allocation_http_replay(
    drafts, authenticated_client, direction, asset
):
    s = drafts
    client, engine, owner = authenticated_client
    assert engine is s["engine"] and owner == s["owner"]
    payload, group = prepared(s, direction=direction, asset=asset, initial=False)
    first = group.model_dump(mode="json")
    first.pop("payment_id")
    first["lines"][0]["payment_amount"] = "44" if asset == "EUR" else "40"
    first["lines"][0]["document_amount"] = "40"
    body = payload.model_dump(mode="json") | {"initial_allocation": first}
    later = group.model_dump(mode="json") | {"id": str(uuid4())}
    later["lines"][0]["payment_amount"] = "66" if asset == "EUR" else "60"
    later["lines"][0]["document_amount"] = "60"
    base = f"/api/v1/ledgers/{s['ledger']}"
    raw, append_raw = json.dumps(body, indent=2).encode(), json.dumps(later).encode()
    headers = {"Idempotency-Key": "fictional-http-payment", "Content-Type": "application/json"}
    response = client.post(base + "/business-payments", content=raw, headers=headers)
    assert response.status_code == 201, response.text
    assert response.headers["cache-control"] == "no-store"
    paid = response.json()
    capacity_path = base + f"/business-documents/{first['lines'][0]['document_id']}/settlement"
    assert client.get(capacity_path).json()["outstanding_amount"] == "60.00"
    detail_path = base + f"/business-payments/{payload.id}"
    detail = client.get(detail_path).json()
    assert detail["receipt"] == paid
    assert detail["unapplied_amount"] == ("66.00" if asset == "EUR" else "60.00")
    assert detail["fees"][0]["amount"] == "1.00"
    append_headers = headers | {"Idempotency-Key": "fictional-http-allocation"}
    response = client.post(
        base + "/business-allocations", content=append_raw, headers=append_headers
    )
    assert response.status_code == 201, response.text
    applied = response.json()
    assert client.get(capacity_path).json()["outstanding_amount"] == "0.00"
    assert client.get(detail_path).json()["unapplied_amount"] == "0.00"
    for identity, receipt in ((first["id"], paid), (later["id"], applied)):
        response = client.get(base + f"/business-allocations/{identity}")
        assert response.headers["cache-control"] == "no-store"
        assert response.json()["receipt"] == receipt
    groups = client.get(base + f"/business-allocations?payment_id={payload.id}").json()
    assert [row["id"] for row in groups] == [first["id"], later["id"]]
    before = snapshot(s)
    assert len(client.get(base + "/business-payments?limit=1&offset=0").json()) == 1
    assert client.get(base + "/business-payments?offset=1").json() == []
    assert client.get(base + f"/operations/{paid['operation_id']}").status_code == 404
    assert snapshot(s) == before
    change_period(s, cutoff="2026-09-30")
    before = snapshot(s)
    assert client.post(base + "/business-payments", content=raw, headers=headers).json() == paid
    assert (
        client.post(
            base + "/business-allocations", content=append_raw, headers=append_headers
        ).json()
        == applied
    )
    response = client.post(
        base + "/business-payments", json=body | {"amount": body["amount"] + ".0"}, headers=headers
    )
    assert (
        response.status_code == 409 and response.json()["detail"]["code"] == "idempotency_conflict"
    )
    assert snapshot(s) == before


def test_scoped_settlement_reads_do_not_expose_other_owned_ledger(drafts, authenticated_client):
    s = drafts
    client, _, _ = authenticated_client
    payload, group = prepared(s)
    base = f"/api/v1/ledgers/{s['ledger']}"
    response = client.post(
        base + "/business-payments",
        json=payload.model_dump(mode="json"),
        headers={"Idempotency-Key": "fictional-scope-payment"},
    )
    assert response.status_code == 201
    other = s["structure"].create_entity(
        s["owner"],
        EntityCreate(
            kind="personal",
            name="Fictional HTTP other ledger",
            base_asset_id="USD",
            template_key="personal_default",
        ),
    )
    base = f"/api/v1/ledgers/{other.ledger.id}"
    before = snapshot(s)
    assert client.get(base + "/business-payments").json() == []
    assert client.get(base + "/business-allocations").json() == []
    for suffix in (
        f"business-payments/{payload.id}",
        f"business-allocations/{group.id}",
        f"business-documents/{group.lines[0].document_id}/settlement",
    ):
        assert client.get(base + "/" + suffix).status_code == 404
    assert snapshot(s) == before
