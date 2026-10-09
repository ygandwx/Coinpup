"""Fictional HTTP rules retain original templates and generated draft evidence."""

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from coinpup_api.business.recurring_job import run_recurring
from coinpup_api.business.schemas import PartyCreate
from coinpup_api.business.service import BusinessService
from coinpup_api.ledger.schemas import EntityCreate
from coinpup_api.ledger.service import LedgerService

from tests.integration.ledger.test_posting_service_database import financial_counts

pytestmark = pytest.mark.integration


def test_rule_http_lifecycle_preserves_generated_identity_and_version_conflicts(
    authenticated_client,
):
    client, engine, owner = authenticated_client
    structure, masters = LedgerService(engine), BusinessService(engine)
    ledger, other = [
        structure.create_entity(
            owner,
            EntityCreate(
                kind="personal",
                name=f"Fictional recurring HTTP {index}",
                base_asset_id="USD",
                template_key="personal_default",
            ),
        ).ledger.id
        for index in range(2)
    ]
    party = masters.create_party(
        owner, ledger, PartyCreate(id=uuid4(), name="Fictional 客户", role="customer")
    )
    category = next(row for row in structure.list_categories(owner, ledger) if row.kind == "income")
    documents = f"/api/v1/ledgers/{ledger}/business-documents"
    source = dict(
        id=str(uuid4()),
        document_kind="invoice",
        party_id=str(party.id),
        asset_id="USD",
        issue_date="2026-01-31",
        notes="Fictional HTTP 模板",
        lines=[
            dict(
                id=str(uuid4()),
                description="Fictional 服务",
                quantity="3.00",
                unit_price="19.99",
                discount_amount="9.97",
                tax_rate_percent="8.2500",
                category_id=str(category.id),
                recognition_date="2026-01-31",
            )
        ],
    )
    response = client.post(documents, json=source)
    assert response.status_code == 201, response.text
    base = f"/api/v1/ledgers/{ledger}/recurring-invoice-rules"
    body = dict(
        id=str(uuid4()),
        name="Fictional 月末",
        timezone_name="Asia/Shanghai",
        anchor_date="2026-01-31",
        frequency="month",
        interval_count=1,
        source_document_id=source["id"],
        source_version=1,
    )
    money = financial_counts(engine)
    response = client.post(base, json=body)
    assert response.status_code == 201, response.text
    first = response.json()
    path = base + "/" + body["id"]
    assert first["next_scheduled_date"] == "2026-01-31" and first["next_index"] == 0
    assert first["template_input"]["lines"][0]["tax_rate_percent"] == "8.2500"
    assert client.get(path).json() == first and client.get(base).json() == [first]
    assert client.get(base).headers["cache-control"] == "no-store"
    assert client.post(base, json=body).status_code == 409
    foreign = f"/api/v1/ledgers/{other}/recurring-invoice-rules/{body['id']}"
    for suffix in ("", "/instances", "/instances/0"):
        assert client.get(foreign + suffix).status_code == 404
    renamed = client.patch(path, json=dict(expected_version=1, name="Fictional renamed"))
    assert renamed.status_code == 200 and renamed.json()["version"] == 2
    assert renamed.json()["template_input"] == first["template_input"]
    result = run_recurring(engine, now=datetime(2026, 4, 1, tzinfo=UTC), per_rule=2)
    assert result["confirmed"] == 2 and result["failures"] == 0
    current = client.get(path).json()
    assert current["version"] == 4 and current["next_scheduled_date"] == "2026-03-31"
    stale = client.patch(path, json=dict(expected_version=2, name="Fictional stale"))
    assert stale.status_code == 409 and stale.json()["detail"]["code"] == "version_conflict"
    instances = client.get(path + "/instances").json()
    assert [row["scheduled_date"] for row in instances] == ["2026-01-31", "2026-02-28"]
    assert client.get(path + "/instances?limit=1&offset=1").json() == instances[1:]
    evidence = instances[0]
    assert client.get(path + "/instances/0").json() == evidence
    generated = client.get(documents + "/" + evidence["id"]).json()
    assert generated["total_amount"] == "54.13" and generated["state"] == "draft"
    edit = evidence["original_input"].copy()
    del edit["id"]
    edit.update(expected_version=1, notes="Fictional 人工修改")
    assert client.put(documents + "/" + evidence["id"], json=edit).status_code == 200
    assert client.get(path + "/instances/0").json() == evidence
    paused = client.patch(path + "/archive", json=dict(expected_version=4, archived=True))
    assert paused.status_code == 200 and paused.json()["version"] == 5
    assert client.get(base + "?include_archived=false").json() == []
    assert client.get(path + "/instances").json() == instances
    assert run_recurring(engine, now=datetime(2026, 4, 1, tzinfo=UTC))["confirmed"] == 0
    restored = client.patch(path + "/archive", json=dict(expected_version=5, archived=False))
    assert restored.status_code == 200 and restored.json()["next_index"] == 2
    assert financial_counts(engine) == money
