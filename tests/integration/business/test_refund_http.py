"""Fictional HTTP refund history and remaining principal survive replay without read writes."""

import json
from uuid import uuid4

import pytest
from coinpup_api.business.refund_reads import BusinessRefundReads
from coinpup_api.ledger.schemas import EntityCreate
from sqlalchemy import event

from tests.integration.business.test_draft_service import drafts as drafts
from tests.integration.business.test_refund_service import setup_refund
from tests.integration.ledger.test_account_classes import snapshot
from tests.integration.ledger.test_period_transactions import change as change_period
from tests.integration.ledger.test_posting_service_database import ledger_setup as ledger_setup

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("direction", ["receipt", "payout"])
@pytest.mark.parametrize("asset,amount", [("USD", "20"), ("EUR", "19.37")])
def test_refund_http_capacity_history_and_exact_original_replay(
    drafts, authenticated_client, direction, asset, amount
):
    s = drafts
    client, _, _ = authenticated_client
    payment, paid, payload = setup_refund(s, direction, asset, amount)
    base = f"/api/v1/ledgers/{s['ledger']}"
    path = base + "/business-refunds"
    capacity = base + f"/business-payments/{payment.id}/refund-capacity"
    before = snapshot(s)
    initial = client.get(capacity)
    assert initial.status_code == 200 and initial.headers["cache-control"] == "no-store"
    assert initial.json()["refunded_amount"] == "0.00"
    assert initial.json()["refundable_amount"] == "20.00"
    assert initial.json()["applied_amount"] == "80.00"
    assert initial.json()["expected_version"] == 1 and initial.json()["source_status"] == "active"
    assert snapshot(s) == before
    raw = json.dumps(payload.model_dump(mode="json"), indent=2).encode()
    headers = {"Idempotency-Key": "fictional-refund-http", "Content-Type": "application/json"}
    response = client.post(path, content=raw, headers=headers)
    assert response.status_code == 201, response.text
    receipt = response.json()
    before = snapshot(s)
    detail = client.get(path + f"/{payload.id}").json()
    assert detail["receipt"] == receipt and detail["source_version"] == 1
    assert detail["reason"] == payload.reason and detail["payment_direction"] == direction
    assert detail["asset_id"] == asset and detail["amount"] == (
        "20.00" if asset == "USD" else amount
    )
    assert detail["payment_asset_id"] == "USD" and detail["payment_amount"] == "20.00"
    assert client.get(path + f"?payment_id={payment.id}").json() == [
        {k: v for k, v in detail.items() if k not in ("source_version", "receipt")}
    ]
    assert client.get(path + "?offset=1").json() == []
    assert client.get(path + f"?payment_id={uuid4()}").json() == []
    current = client.get(capacity).json()
    assert current == initial.json() | {"refunded_amount": "20.00", "refundable_amount": "0.00"}
    assert client.get(base + f"/business-payments/{payment.id}").json()[
        "receipt"
    ] == paid.model_dump(mode="json")
    assert snapshot(s) == before
    change_period(s, cutoff="2026-10-02")
    before = snapshot(s)
    assert client.post(path, content=raw, headers=headers).json() == receipt
    conflict = client.post(
        path, json=payload.model_dump(mode="json") | {"reason": "different"}, headers=headers
    )
    assert (
        conflict.status_code == 409 and conflict.json()["detail"]["code"] == "idempotency_conflict"
    )
    assert snapshot(s) == before


def test_refund_queries_are_scoped_and_list_query_count_is_bounded(drafts, authenticated_client):
    s = drafts
    client, _, _ = authenticated_client
    payment, _, payload = setup_refund(s)
    other = (
        s["structure"]
        .create_entity(
            s["owner"],
            EntityCreate(
                kind="personal",
                name="Fictional isolated refund",
                base_asset_id="USD",
                template_key="personal_default",
            ),
        )
        .ledger.id
    )
    base = f"/api/v1/ledgers/{s['ledger']}"
    other_path = f"/api/v1/ledgers/{other}"
    reader = BusinessRefundReads(s["engine"])

    def count_queries():
        statements = []

        def capture(_conn, _cursor, statement, _parameters, _context, _many):
            if statement.lstrip().upper().startswith("SELECT"):
                statements.append(statement)

        event.listen(s["engine"], "before_cursor_execute", capture)
        try:
            reader.list_refunds(s["owner"], s["ledger"])
        finally:
            event.remove(s["engine"], "before_cursor_execute", capture)
        return len(statements)

    assert count_queries() == 4  # owner, ledger, entity, single joined page
    response = client.post(
        base + "/business-refunds",
        json=payload.model_dump(mode="json"),
        headers={"Idempotency-Key": "fictional-refund-scope"},
    )
    assert response.status_code == 201, response.text
    assert count_queries() == 4
    before = snapshot(s)
    assert client.get(other_path + "/business-refunds").json() == []
    assert client.get(other_path + f"/business-refunds/{payload.id}").status_code == 404
    assert (
        client.get(other_path + f"/business-payments/{payment.id}/refund-capacity").status_code
        == 404
    )
    assert snapshot(s) == before
