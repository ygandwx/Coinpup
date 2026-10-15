"""Fictional return HTTP lifecycle keeps receipts, scope and read-only capacities exact."""

import json
from types import SimpleNamespace
from uuid import uuid4

import pytest
from coinpup_api.business.allocation_return_reads import BusinessAllocationReturnReads
from coinpup_api.ledger.models import CommandReceipt
from coinpup_api.ledger.schemas import EntityCreate
from coinpup_api.ledger.service import LedgerError
from sqlalchemy.orm import Session

from tests.integration.business.test_allocation_return_service import setup_return, submit_return
from tests.integration.business.test_draft_service import drafts as drafts
from tests.integration.ledger.test_account_classes import snapshot
from tests.integration.ledger.test_period_transactions import change as change_period
from tests.integration.ledger.test_posting_service_database import ledger_setup as ledger_setup

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("direction", ["receipt", "payout"])
@pytest.mark.parametrize("asset", ["USD", "EUR"])
def test_return_http_history_capacity_original_replay(
    drafts, authenticated_client, direction, asset
):
    s = drafts
    client, _, _ = authenticated_client
    payment, paid, allocation, payload = setup_return(s, direction=direction, asset=asset)
    base = f"/api/v1/ledgers/{s['ledger']}"
    path = base + "/business-allocation-returns"
    capacity = base + f"/business-allocations/{allocation.id}/return-capacity"
    original = client.get(base + f"/business-allocations/{allocation.id}").json()
    before = snapshot(s)
    response = client.get(capacity)
    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    line = response.json()["lines"][0]
    assert line["source_allocation_line_id"] == str(payload.lines[0].source_allocation_line_id)
    assert line["expected_version"] == 1 and line["source_status"] == "active"
    assert line["returned_payment_amount"] == line["returned_document_amount"] == "0.00"
    assert line["document_asset_id"] == asset
    assert snapshot(s) == before
    headers = {"Idempotency-Key": "fictional-return-http", "Content-Type": "application/json"}
    raw = json.dumps(payload.model_dump(mode="json"), indent=2).encode()
    response = client.post(path, content=raw, headers=headers)
    assert response.status_code == 201, response.text
    receipt = response.json()
    assert receipt["postings"][0]["document_amount"] == "20.000000000000000000"
    before = snapshot(s)
    detail = client.get(path + f"/{payload.id}").json()
    assert detail["receipt"] == receipt and detail["reason"] == payload.reason
    assert detail["line_count"] == 1
    assert detail["lines"][0]["source_allocation_id"] == str(allocation.id)
    assert detail["lines"][0]["document_asset_id"] == asset
    assert detail["lines"][0]["document_amount"] == "20.00"
    assert detail["lines"][0]["description"]
    assert detail["payment_amount"] == ("22.00" if asset == "EUR" else "20.00")
    listed = client.get(path + f"?payment_id={payment.id}").json()
    assert listed == [{k: v for k, v in detail.items() if k not in ("receipt", "lines")}]
    assert client.get(path + "?offset=1").json() == []
    assert client.get(path + f"?payment_id={uuid4()}").json() == []
    line = client.get(capacity).json()["lines"][0]
    assert line["remaining_payment_amount"] == ("88.00" if asset == "EUR" else "80.00")
    assert line["remaining_document_amount"] == "80.00"
    assert client.get(base + f"/business-allocations/{allocation.id}").json() == original
    assert client.get(base + f"/business-payments/{payment.id}").json()[
        "receipt"
    ] == paid.model_dump(mode="json")
    assert snapshot(s) == before
    change_period(s, cutoff="2026-10-31")
    before = snapshot(s)
    assert client.post(path, content=raw, headers=headers).json() == receipt
    assert client.get(path + f"/{payload.id}").json() == detail
    changed = payload.model_dump(mode="json")
    changed["lines"][0]["document_amount"] = "20.0"
    assert client.post(path, json=changed, headers=headers).status_code == 409
    assert snapshot(s) == before


def test_returns_are_scoped_to_owned_ledger(drafts, authenticated_client):
    s = drafts
    client, _, _ = authenticated_client
    _, _, allocation, payload = setup_return(s)
    submit_return(s, payload)
    other = s["structure"].create_entity(
        s["owner"],
        EntityCreate(
            kind="personal",
            name="Fictional return scope",
            base_asset_id="USD",
            template_key="personal_default",
        ),
    )
    base = f"/api/v1/ledgers/{other.ledger.id}"
    before = snapshot(s)
    assert client.get(base + "/business-allocation-returns").json() == []
    for path in (
        f"business-allocation-returns/{payload.id}",
        f"business-allocations/{allocation.id}/return-capacity",
    ):
        assert client.get(base + "/" + path).status_code == 404
    response = client.post(
        base + "/business-allocation-returns",
        json=payload.model_dump(mode="json"),
        headers={"Idempotency-Key": "fictional-wrong-return-ledger"},
    )
    assert response.status_code == 404
    assert snapshot(s) == before


@pytest.mark.parametrize("fault", ["amount", "anchor", "status"])
def test_return_read_cannot_accept_corrupt_original_receipt(drafts, monkeypatch, fault):
    s = drafts
    _, _, _, payload = setup_return(s)
    submit_return(s, payload)
    original = Session.get

    def corrupt(self, entity, *args, **kwargs):
        row = original(self, entity, *args, **kwargs)
        if entity is CommandReceipt and row is not None:
            response = json.loads(json.dumps(row.response))
            if fault == "amount":
                response["postings"][0]["payment_amount"] = "1"
            return SimpleNamespace(
                response=response,
                hash_version=2,
                response_status=200 if fault == "status" else 201,
                operation_id=uuid4() if fault == "anchor" else row.operation_id,
            )
        return row

    monkeypatch.setattr(Session, "get", corrupt)
    before = snapshot(s)
    with pytest.raises(LedgerError) as error:
        BusinessAllocationReturnReads(s["engine"]).get_return(s["owner"], s["ledger"], payload.id)
    assert (error.value.code, error.value.status) == ("allocation_return_integrity", 503)
    assert snapshot(s) == before
