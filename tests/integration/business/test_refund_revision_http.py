"""Real PostgreSQL and signed-in HTTP exercise each fixed refund receipt."""

import json
from uuid import uuid4

import pytest
from coinpup_api.business.refund_revisions import BusinessRefundRevisionService
from coinpup_api.business.refund_schemas import BusinessRefundResponse
from coinpup_api.ledger.schemas import EntityCreate
from sqlalchemy.exc import SQLAlchemyError

from tests.integration.business.test_draft_service import drafts as drafts
from tests.integration.business.test_refund_revision_service import correction
from tests.integration.business.test_refund_service import setup_refund
from tests.integration.ledger.test_account_classes import snapshot
from tests.integration.ledger.test_period_transactions import change as change_period
from tests.integration.ledger.test_posting_service_database import ledger_setup as ledger_setup

pytestmark = pytest.mark.integration


def send(client, path, body, key, *, headers=None):
    raw = json.dumps(body, ensure_ascii=False, indent=2).encode()
    headers = {"Idempotency-Key": key, "Content-Type": "application/json"} | (headers or {})
    result = client.post(path, content=raw, headers=headers)
    assert result.headers["cache-control"] == "no-store"
    return result


def create(s, client, direction="receipt"):
    payment, _, command = setup_refund(s, direction, asset="EUR", amount="18.37")
    path = f"/api/v1/ledgers/{s['ledger']}/business-refunds"
    body = command.model_dump(mode="json")
    response = send(client, path, body, "fictional-create-refund")
    assert response.status_code == 201, response.text
    return payment, path, body, BusinessRefundResponse.model_validate(response.json())


@pytest.mark.parametrize("direction", ["receipt", "payout"])
def test_all_receipts_remain_exact_after_refund_changes_and_closed_period(
    drafts, authenticated_client, direction
):
    s = drafts
    client, _, _ = authenticated_client
    payment, path, body, original = create(s, client, direction)
    detail = f"{path}/{original.id}"
    legacy = client.get(detail).json()
    assert legacy["receipt"] == original.model_dump(mode="json")
    capacity = f"/api/v1/ledgers/{s['ledger']}/business-payments/{payment.id}/refund-capacity"
    first = correction(s, original, amount="7.00", payment_amount="7.00")
    key = "fictional-http-refund-correction"
    corrected = send(client, detail + "/corrections", first, key)
    assert corrected.status_code == 200, corrected.text
    receipt = corrected.json()
    assert receipt["previous"]["allocation_return_ids"] == body["allocation_return_ids"]
    assert receipt["replacement"]["allocation_return_ids"] == []
    assert receipt["previous_journal_id"] == str(original.journal_id)
    assert receipt["operation_id"] == str(original.operation_id)
    assert client.get(capacity).json()["refunded_amount"] == "7.00"
    changed = first | {"replacement": first["replacement"] | {"amount": "7.0"}}
    before = snapshot(s)
    for target, request, reused in (
        (detail + "/corrections", changed, key),
        (detail.replace(str(original.id), str(uuid4())) + "/corrections", first, key),
        (detail + "/cancellations", {"expected_version": 1, "reason": "Fictional"}, key),
    ):
        r = send(client, target, request, reused)
        assert r.status_code == 409 and r.json()["detail"]["code"] == "idempotency_conflict"
    assert snapshot(s) == before
    end = {"expected_version": 2, "reason": "Fictional final cancellation 中文"}
    cancel_key = "fictional-cancel-refund"
    cancelled = send(client, detail + "/cancellations", end, cancel_key)
    assert cancelled.status_code == 200, cancelled.text
    last = cancelled.json()
    assert last["version"] == 3 and last["previous"] == receipt["replacement"]
    assert last["replacement"] is None and last["replacement_journal_id"] is None
    assert client.get(capacity).json()["refunded_amount"] == "0.00"
    change_period(s, cutoff="2026-10-05")
    before = snapshot(s)
    for target, request, original_key, status, accepted in (
        (path, body, "fictional-create-refund", 201, original.model_dump(mode="json")),
        (detail + "/corrections", first, key, 200, receipt),
        (detail + "/cancellations", end, cancel_key, 200, last),
    ):
        replay = send(client, target, request, original_key)
        assert replay.status_code == status and replay.json() == accepted
    assert client.get(detail).json() == legacy and snapshot(s) == before


@pytest.mark.parametrize("action", ["correct", "cancel"])
def test_unknown_commit_replays_only_in_original_ledger_after_login(
    drafts, authenticated_client, monkeypatch, action
):
    s = drafts
    client, _, _ = authenticated_client
    _, base, _, refund = create(s, client)
    suffix = "corrections" if action == "correct" else "cancellations"
    path = f"{base}/{refund.id}/{suffix}"
    cmd = (
        correction(s, refund)
        if action == "correct"
        else {"expected_version": 1, "reason": "Fictional cancellation"}
    )
    key = "fictional-lost-result"
    other = s["structure"].create_entity(
        s["owner"],
        EntityCreate(
            kind="personal",
            name="Fictional isolated other ledger",
            base_asset_id="USD",
            template_key="personal_default",
        ),
    )
    before = snapshot(s)
    for target in (
        path.replace(str(refund.id), str(uuid4())),
        path.replace(str(s["ledger"]), str(other.ledger.id)),
    ):
        denied = send(client, target, cmd, key)
        assert denied.status_code == 404 and denied.json()["detail"]["code"] == "not_found"
    for headers in ({"Origin": "https://fictional.invalid"}, {"X-CSRF-Token": "fictional-wrong"}):
        assert send(client, path, cmd, key, headers=headers).status_code == 403
    assert snapshot(s) == before
    service = getattr(BusinessRefundRevisionService, action)
    committed = []

    def lost(self, *args, **kwargs):
        committed.append(service(self, *args, **kwargs).model_dump(mode="json"))
        raise SQLAlchemyError("Fictional failure delivering the response")

    monkeypatch.setattr(BusinessRefundRevisionService, action, lost)
    failure = send(client, path, cmd, key)
    assert failure.status_code == 503 and failure.json()["detail"]["code"] == "business_unavailable"
    assert len(committed) == 1 and snapshot(s) != before
    monkeypatch.setattr(BusinessRefundRevisionService, action, service)
    saved = snapshot(s)
    session = client.cookies.get("coinpup_session")
    client.cookies.clear()
    assert send(client, path, cmd, key).status_code == 401
    client.cookies.set("coinpup_session", session)
    conflict = send(client, path, cmd | {"reason": "Fictional distinct retry"}, key)
    assert (
        conflict.status_code == 409 and conflict.json()["detail"]["code"] == "idempotency_conflict"
    )
    replay = send(client, path, cmd, key)
    assert replay.status_code == 200 and replay.json() == committed[0]
    assert snapshot(s) == saved
