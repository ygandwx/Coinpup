"""Fictional real HTTP credits, independent capacities and private original receipts."""

import json
from uuid import uuid4

import pytest
from coinpup_api.business.credit_reads import BusinessCreditReads
from coinpup_api.ledger.schemas import AssetUpdate, EntityCreate, EntityUpdate
from sqlalchemy import event

from tests.integration.business.test_credit_service import setup_credit
from tests.integration.business.test_draft_service import drafts as drafts
from tests.integration.ledger.test_account_classes import snapshot
from tests.integration.ledger.test_period_transactions import change as change_period
from tests.integration.ledger.test_posting_service_database import ledger_setup as ledger_setup

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("kind", ["invoice", "bill"])
def test_credit_http_preserves_manual_components_original_receipt_and_historical_replay(
    drafts, authenticated_client, kind
):
    s = drafts
    client, _, _ = authenticated_client
    document, _, confirmed, payload = setup_credit(s, kind, count=2)
    base = f"/api/v1/ledgers/{s['ledger']}"
    path = base + "/business-credits"
    capacity = base + f"/business-documents/{document.id}/credit-capacity"
    before = snapshot(s)
    initial = client.get(capacity)
    assert initial.status_code == 200 and initial.headers["cache-control"] == "no-store"
    assert len(initial.json()["lines"]) == 2
    for line in initial.json()["lines"]:
        assert (line["net_amount"], line["tax_amount"], line["total_amount"]) == (
            "100.00",
            "10.00",
            "110.00",
        )
        assert line["available_gross_amount"] == "110.00"
        assert line["credited_net_amount"] == line["credited_tax_amount"] == "0.00"
    assert client.get(capacity + "?limit=1&offset=1").json()["lines"] == initial.json()["lines"][1:]
    assert client.get(capacity + "?offset=2").json()["lines"] == []
    assert snapshot(s) == before
    raw = json.dumps(payload.model_dump(mode="json"), indent=2).encode()
    headers = {"Idempotency-Key": "fictional-credit-http", "Content-Type": "application/json"}
    response = client.post(path, content=raw, headers=headers)
    assert response.status_code == 201, response.text
    receipt = response.json()
    before = snapshot(s)
    detail = client.get(path + f"/{payload.id}")
    assert detail.status_code == 200 and detail.headers["cache-control"] == "no-store"
    detail = detail.json()
    assert detail["receipt"] == receipt and detail["source_version"] == confirmed.document_version
    assert detail["document_kind"] == kind and detail["reason"] == payload.reason
    assert client.get(path + f"?document_id={document.id}").json() == [
        {key: value for key, value in detail.items() if key not in ("source_version", "receipt")}
    ]
    assert client.get(path + "?offset=1").json() == []
    assert client.get(path + f"?document_id={uuid4()}").json() == []
    for line in client.get(capacity).json()["lines"]:
        assert line["net_amount"] == "100.00" and line["tax_amount"] == "10.00"
        assert line["credited_net_amount"] == "17.23" and line["credited_tax_amount"] == "2.77"
        assert line["remaining_net_amount"] == "82.77" and line["remaining_tax_amount"] == "7.23"
        assert line["applied_amount"] == "0.00" and line["available_gross_amount"] == "90.00"
    assert snapshot(s) == before
    change_period(s, cutoff="2026-10-21")
    s["structure"].update_asset(s["owner"], "USD", AssetUpdate(expected_version=1, enabled=False))
    s["structure"].update_entity(
        s["owner"], s["entity"].id, EntityUpdate(expected_version=1, archived=True)
    )
    before = snapshot(s)
    assert client.post(path, content=raw, headers=headers).json() == receipt
    assert client.get(path + f"/{payload.id}").json() == detail
    assert client.get(capacity).status_code == 200
    different = client.post(
        path, json=payload.model_dump(mode="json") | {"reason": "different"}, headers=headers
    )
    assert different.status_code == 409
    assert different.json()["detail"]["code"] == "idempotency_conflict"
    assert snapshot(s) == before


def test_credit_read_scope_and_constant_queries(drafts, authenticated_client):
    s = drafts
    client, _, _ = authenticated_client
    document, _, _, payload = setup_credit(s, count=2)
    other = (
        s["structure"]
        .create_entity(
            s["owner"],
            EntityCreate(
                kind="personal",
                name="Fictional isolated credit",
                base_asset_id="USD",
                template_key="personal_default",
            ),
        )
        .ledger.id
    )
    base = f"/api/v1/ledgers/{s['ledger']}"
    foreign = f"/api/v1/ledgers/{other}"
    reader = BusinessCreditReads(s["engine"])

    def queries(operation):
        statements = []

        def capture(_conn, _cursor, statement, _parameters, _context, _many):
            if statement.lstrip().upper().startswith("SELECT"):
                statements.append(statement)

        event.listen(s["engine"], "before_cursor_execute", capture)
        try:
            operation()
        finally:
            event.remove(s["engine"], "before_cursor_execute", capture)
        return len(statements)

    assert queries(lambda: reader.list_credits(s["owner"], s["ledger"])) == 4
    assert queries(lambda: reader.capacity(s["owner"], s["ledger"], document.id)) == 6
    response = client.post(
        base + "/business-credits",
        json=payload.model_dump(mode="json"),
        headers={"Idempotency-Key": "fictional-credit-scope"},
    )
    assert response.status_code == 201, response.text
    assert queries(lambda: reader.list_credits(s["owner"], s["ledger"])) == 4
    assert queries(lambda: reader.capacity(s["owner"], s["ledger"], document.id)) == 6
    before = snapshot(s)
    assert client.get(foreign + "/business-credits").json() == []
    assert client.get(foreign + f"/business-credits/{payload.id}").status_code == 404
    assert (
        client.get(foreign + f"/business-documents/{document.id}/credit-capacity").status_code
        == 404
    )
    for query in ("limit=0", "limit=201", "offset=-1", "offset=100001"):
        assert (
            client.get(
                base + f"/business-documents/{document.id}/credit-capacity?{query}"
            ).status_code
            == 422
        )
    assert snapshot(s) == before
