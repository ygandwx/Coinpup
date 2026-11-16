"""Authenticated HTTP preserves original return facts, order and v2 replay across lost responses."""

from uuid import uuid4

import pytest
from coinpup_api.business.allocation_return_revisions import BusinessAllocationReturnRevisionService
from coinpup_api.ledger.schemas import EntityCreate, EntityUpdate
from sqlalchemy.exc import SQLAlchemyError

from tests.integration.business.test_allocation_return_service import setup_return
from tests.integration.business.test_draft_service import drafts as drafts
from tests.integration.business.test_refund_revision_http import send
from tests.integration.business.test_return_revision_service import cancel, command
from tests.integration.ledger.test_account_classes import snapshot
from tests.integration.ledger.test_posting_service_database import ledger_setup as ledger_setup

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("direction,asset", [("receipt", "USD"), ("payout", "EUR")])
def test_original_and_revised_http_receipts_survive_reordering_and_final_cancellation(
    drafts, authenticated_client, direction, asset
):
    s = drafts
    client, _, _ = authenticated_client
    _, _, group, body = setup_return(s, direction=direction, asset=asset, count=2)
    path = f"/api/v1/ledgers/{s['ledger']}/business-allocation-returns"
    submitted = body.model_dump(mode="json")
    first = send(client, path, submitted, "fictional-http-201")
    assert first.status_code == 201
    target = f"{path}/{body.id}"
    created = first.json()
    detail = client.get(target).json()
    assert detail["receipt"] == created
    change = command(body).model_dump(mode="json")
    change["replacement"]["lines"][0]["payment_amount"] += ".0"
    edited = send(client, target + "/corrections", change, "fictional-http-200a")
    assert edited.status_code == 200, edited.text
    correction = edited.json()
    assert [m["previous"]["journal_id"] for m in correction["members"]] == [
        m["journal_id"] for m in reversed(created["postings"])
    ]
    conflict = command(body).model_dump(mode="json")
    rejected = send(client, target + "/corrections", conflict, "fictional-http-200a")
    assert (
        rejected.status_code == 409 and rejected.json()["detail"]["code"] == "idempotency_conflict"
    )
    end = cancel(2).model_dump(mode="json")
    last = send(client, target + "/cancellations", end, "fictional-http-200b")
    assert last.status_code == 200
    cancelled = last.json()
    assert cancelled["replacement"] is None and all(
        m["action"] == "cancel" and not m["replacement"] for m in cancelled["members"]
    )
    capacity = client.get(
        f"/api/v1/ledgers/{s['ledger']}/business-allocations/{group.id}/return-capacity"
    ).json()
    assert {m["returned_document_amount"] for m in capacity["lines"]} == {"0.00"}
    s["structure"].update_entity(
        s["owner"], s["entity"].id, EntityUpdate(expected_version=1, archived=True)
    )
    before = snapshot(s)
    for url, payload, key, status, expected in [
        (path, submitted, "fictional-http-201", 201, created),
        (target + "/corrections", change, "fictional-http-200a", 200, correction),
        (target + "/cancellations", end, "fictional-http-200b", 200, cancelled),
    ]:
        actual = send(client, url, payload, key)
        assert (actual.status_code, actual.json()) == (status, expected)
    assert client.get(target).json() == detail and snapshot(s) == before


@pytest.mark.parametrize("action", ["correct", "cancel"])
def test_command_access_scope_and_same_raw_retry_after_committed_unknown_result(
    drafts, authenticated_client, monkeypatch, action
):
    s = drafts
    client, _, _ = authenticated_client
    _, _, _, body = setup_return(s)
    path = f"/api/v1/ledgers/{s['ledger']}/business-allocation-returns"
    created = send(client, path, body.model_dump(mode="json"), "fictional-return-first")
    assert created.status_code == 201
    end = action == "cancel"
    suffix = "cancellations" if end else "corrections"
    payload = (cancel(1) if end else command(body)).model_dump(mode="json")
    route = f"{path}/{body.id}/{suffix}"
    key = "fictional-response-lost"
    other = s["structure"].create_entity(
        s["owner"],
        EntityCreate(
            kind="personal",
            name="Fictional other ledger",
            base_asset_id="USD",
            template_key="personal_default",
        ),
    )
    before = snapshot(s)
    for target in [
        route.replace(str(body.id), str(uuid4())),
        route.replace(str(s["ledger"]), str(other.ledger.id)),
    ]:
        rejected = send(client, target, payload, key)
        assert rejected.status_code == 404 and rejected.json()["detail"]["code"] == "not_found"
    for headers in [{"Origin": "https://fictional.invalid"}, {"X-CSRF-Token": "fictional-wrong"}]:
        assert send(client, route, payload, key, headers=headers).status_code == 403
    assert snapshot(s) == before
    invoke = getattr(BusinessAllocationReturnRevisionService, action)
    committed = []

    def lost(service, *args, **kwargs):
        committed.append(invoke(service, *args, **kwargs).model_dump(mode="json"))
        raise SQLAlchemyError("Fictional confidential SQL detail")

    with monkeypatch.context() as patch:
        patch.setattr(BusinessAllocationReturnRevisionService, action, lost)
        failed = send(client, route, payload, key)
    assert failed.status_code == 503 and failed.json()["detail"]["code"] == "business_unavailable"
    assert "SQL" not in failed.text
    before = snapshot(s)
    rejected = send(client, route, payload | {"reason": "Fictional changed intent"}, key)
    assert (
        rejected.status_code == 409 and rejected.json()["detail"]["code"] == "idempotency_conflict"
    )
    recovered = send(client, route, payload, key)
    assert (recovered.status_code, [recovered.json()]) == (200, committed) and snapshot(s) == before
