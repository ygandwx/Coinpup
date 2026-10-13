"""Real authenticated fictional bank confirmation returns and replays its distinct envelope."""

import json

import pytest

from tests.integration.business.test_draft_service import drafts as drafts
from tests.integration.ledger.test_account_classes import snapshot
from tests.integration.ledger.test_posting_service_database import ledger_setup as ledger_setup
from tests.integration.ocr.test_payment_confirmation_service import setup

pytestmark = pytest.mark.integration


def test_link_payment_http_envelope_keeps_original_intent_and_permanent_read(
    drafts, tmp_path, authenticated_client
):
    s = setup(drafts, tmp_path)
    client, _, _ = authenticated_client
    base = f"/api/v1/ledgers/{s['ledger']}/ocr-drafts/{s['draft']}"
    raw = json.dumps(s["body"].model_dump(mode="json", exclude_unset=True), indent=2).encode()
    headers = {"content-type": "application/json"}
    response = client.post(base + "/confirmations", content=raw, headers=headers)
    assert response.status_code == 201, response.text
    value = response.json()
    assert value["action"] == "link_payment" and "operation" not in value
    assert value["payment"] == s["payment"].model_dump(mode="json")
    assert response.headers["cache-control"] == "no-store"
    before = snapshot(s)
    repeated = client.post(base + "/confirmations", content=raw, headers=headers)
    assert repeated.status_code == 201 and repeated.content == response.content
    read = client.get(base + "/confirmation")
    assert read.status_code == 200 and read.json() == value
    assert read.headers["cache-control"] == "no-store"
    assert client.get(base + "/review").json()["review"]["entry"]["kind"] == "link_payment"
    changed = json.loads(raw) | {"duplicate_ack": False}
    conflict = client.post(base + "/confirmations", json=changed)
    assert (
        conflict.status_code == 409 and conflict.json()["detail"]["code"] == "idempotency_conflict"
    )
    assert snapshot(s) == before


def test_legacy_link_never_uses_payment_identity_as_a_financial_creation(
    drafts, tmp_path, authenticated_client
):
    s = setup(drafts, tmp_path)
    client, _, _ = authenticated_client
    base = f"/api/v1/ledgers/{s['ledger']}/ocr-drafts/{s['draft']}"
    body = s["body"].model_dump(mode="json")
    body["entry"] = dict(kind="link", operation_id=str(s["payment"].id), expected_version=1)
    before = snapshot(s)
    response = client.post(base + "/confirmations", json=body)
    assert response.status_code == 404
    assert snapshot(s) == before
