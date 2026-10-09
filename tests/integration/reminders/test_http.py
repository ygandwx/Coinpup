"""Fictional reminder API transitions preserve dates, isolation and immutable evidence."""

from uuid import uuid4

import pytest
from coinpup_api.ledger.schemas import EntityCreate
from coinpup_api.ledger.service import LedgerService
from coinpup_api.reminders.catalog import VERSION

from tests.integration.ledger.test_posting_service_database import financial_counts

pytestmark = pytest.mark.integration


def test_http_reminder_lifecycle_and_versioned_history(authenticated_client):
    client, engine, owner = authenticated_client
    service = LedgerService(engine)
    ledger, other = [
        service.create_entity(
            owner,
            EntityCreate(
                kind="personal", name=f"Fictional reminder HTTP {index}", base_asset_id="USD"
            ),
        ).ledger.id
        for index in range(2)
    ]
    base = f"/api/v1/ledgers/{ledger}/reminders"
    rules = client.get(base + "/rules")
    assert rules.status_code == 200 and len(rules.json()) == 6
    before = financial_counts(engine)
    rule = dict(
        rule_id="certificate.expiry",
        rule_version=VERSION,
        expiry_date="2027-01-31",
        applicability_confirmed=True,
    )
    body = dict(
        id=str(uuid4()),
        event_kind="certificate",
        title="Fictional 证件",
        notes="Fictional source",
        rule=rule,
    )
    response = client.post(base, json=body)
    assert response.status_code == 201, response.text
    original = response.json()
    path = base + "/" + body["id"]
    assert original["effective_date"] == "2027-01-31"
    assert original["evaluation"]["rule"]["version"] == VERSION
    assert original["evaluation"]["inputs"]["entity_kind"] == "personal"
    assert client.get(path).json() == original
    assert client.get(base).json() == [original]
    assert client.get(path).headers["cache-control"] == "no-store"
    assert client.post(base, json=body).status_code == 409
    edited = client.patch(path, json=dict(expected_version=1, title="Fictional 已核对", notes=None))
    assert edited.status_code == 200 and edited.json()["version"] == 2
    response = client.patch(
        path + "/manual-date",
        json=dict(
            expected_version=2, manual_due_date="2027-02-10", reason="Fictional accountant notice"
        ),
    )
    assert response.status_code == 200 and response.json()["effective_date"] == "2027-02-10"
    response = client.post(path + "/transition", json=dict(expected_version=3, action="complete"))
    assert response.status_code == 200 and response.json()["completed"] is True
    rule["expiry_date"] = "2027-02-15"
    response = client.post(path + "/recalculate", json=dict(expected_version=4, rule=rule))
    assert response.status_code == 200, response.text
    row = response.json()
    assert row["completed"] and row["calculated_date"] == "2027-02-15"
    assert row["effective_date"] == "2027-02-10"
    response = client.patch(
        path + "/manual-date",
        json=dict(expected_version=5, manual_due_date=None, reason="Fictional withdrawal"),
    )
    assert response.status_code == 200 and response.json()["effective_date"] == "2027-02-15"
    assert client.get(base + "?include_completed=false").json() == []
    assert (
        client.post(
            path + "/transition", json=dict(expected_version=6, action="archive")
        ).status_code
        == 200
    )
    assert client.get(base + "?include_archived=false").json() == []
    history = client.get(path + "/revisions").json()
    assert [row["version"] for row in history] == list(range(1, 8))
    assert history[2]["snapshot"]["manual_reason"] == "Fictional accountant notice"
    assert client.get(path + "/revisions?limit=2&offset=1").json() == history[1:3]
    response = client.post(path + "/transition", json=dict(expected_version=7, action="restore"))
    assert response.status_code == 200 and response.json()["completed"] is True
    response = client.post(path + "/transition", json=dict(expected_version=8, action="reopen"))
    assert response.status_code == 200 and response.json()["completed"] is False
    response = client.patch(path, json=dict(expected_version=1, title="Fictional stale"))
    assert response.status_code == 409 and response.json()["detail"]["code"] == "version_conflict"
    foreign = f"/api/v1/ledgers/{other}/reminders/{body['id']}"
    for suffix in ("", "/revisions"):
        assert client.get(foreign + suffix).status_code == 404
    assert (
        client.patch(foreign, json=dict(expected_version=9, title="Fictional denied")).status_code
        == 404
    )
    assert financial_counts(engine) == before


def test_api_keeps_missing_unknown_manual_and_invalid_rule_states_distinct(authenticated_client):
    client, engine, owner = authenticated_client
    ledger = (
        LedgerService(engine)
        .create_entity(
            owner,
            EntityCreate(kind="personal", name="Fictional missing input", base_asset_id="USD"),
        )
        .ledger.id
    )
    base = f"/api/v1/ledgers/{ledger}/reminders"
    body = dict(
        id=str(uuid4()),
        title="Fictional missing",
        event_kind="certificate",
        rule=dict(rule_id="certificate.expiry", rule_version=VERSION),
    )
    response = client.post(base, json=body)
    assert response.status_code == 201
    assert response.json()["evaluation_status"] == "missing_parameters"
    assert response.json()["effective_date"] is None
    body["id"] = str(uuid4())
    body["rule"]["expiry_date"] = "2027-01-31"
    response = client.post(base, json=body)
    assert response.status_code == 201
    assert response.json()["evaluation_status"] == "needs_verification"
    assert response.json()["effective_date"] is None
    body["id"] = str(uuid4())
    body["rule"]["rule_version"] = "fictional-unpublished"
    response = client.post(base, json=body)
    assert (
        response.status_code == 422
        and response.json()["detail"]["code"] == "reminder_rule_version_unknown"
    )
    body.update(
        id=str(uuid4()),
        event_kind="tax",
        rule=None,
        manual_due_date="2027-02-01",
        manual_reason="Fictional manual notice",
    )
    response = client.post(base, json=body)
    assert response.status_code == 201
    assert response.json()["effective_date"] == "2027-02-01"
    assert response.json()["calculated_date"] is None
    body.update(id=str(uuid4()), manual_due_date="2027-02-01T00:00:00Z")
    assert client.post(base, json=body).status_code == 422
    assert len(client.get(base).json()) == 3
