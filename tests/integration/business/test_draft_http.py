"""Real draft HTTP lifecycle uses fictional data and exact source strings."""

from copy import deepcopy
from uuid import uuid4

import pytest
from coinpup_api.business.schemas import PartyCreate, ProjectCreate
from coinpup_api.business.service import BusinessService
from coinpup_api.ledger.schemas import EntityCreate
from coinpup_api.ledger.service import LedgerService

from tests.integration.ledger.test_posting_service_database import financial_counts

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("kind", ["invoice", "bill"])
def test_draft_http_exact_lifecycle_and_unknown_result_recovery(authenticated_client, kind):
    client, engine, owner = authenticated_client
    structure, masters = LedgerService(engine), BusinessService(engine)
    ledger, other = [
        structure.create_entity(
            owner,
            EntityCreate(
                kind="personal",
                name=f"Fictional draft HTTP {index}",
                base_asset_id="USD",
                template_key="personal_default",
            ),
        ).ledger.id
        for index in range(2)
    ]
    party = masters.create_party(
        owner, ledger, PartyCreate(id=uuid4(), name="Fictional 往来", role="both")
    )
    project = masters.create_project(
        owner, ledger, ProjectCreate(id=uuid4(), name="Fictional 项目")
    )
    category = next(
        row
        for row in structure.list_categories(owner, ledger)
        if row.kind == ("income" if kind == "invoice" else "expense")
    )
    base = f"/api/v1/ledgers/{ledger}/business-documents"
    body = dict(
        id=str(uuid4()),
        document_kind=kind,
        party_id=str(party.id),
        asset_id="USD",
        issue_date="2026-10-09",
        notes="Fictional HTTP 备注",
        lines=[
            dict(
                id=str(uuid4()),
                description="Fictional 服务",
                quantity="3.00",
                unit_price="19.99",
                discount_amount="9.97",
                tax_rate_percent="8.2500",
                category_id=str(category.id),
                project_id=str(project.id),
                recognition_date="2026-09-30",
            )
        ],
    )
    financial = financial_counts(engine)
    response = client.post(base, json=body)
    assert response.status_code == 201, response.text
    assert response.headers["cache-control"] == "no-store"
    original = response.json()
    assert original["id"] == body["id"] and original["ledger_id"] == str(ledger)
    assert (original["net_amount"], original["tax_amount"], original["total_amount"]) == (
        "50.00",
        "4.13",
        "54.13",
    )
    assert original["lines"][0]["quantity"] == "3.00"
    assert original["lines"][0]["tax_rate_percent"] == "8.2500"
    path = base + "/" + body["id"]
    assert client.get(path).json() == original
    summary = {key: value for key, value in original.items() if key != "lines"}
    assert client.get(base).json() == [summary]
    assert (
        client.get(base).json()
        == client.get(base + "?include_archived=true&limit=100&offset=0").json()
    )
    duplicate = client.post(base, json=body)
    assert duplicate.status_code == 409 and duplicate.json()["detail"]["code"] == "duplicate_record"
    assert client.get(path).json() == original
    foreign_path = f"/api/v1/ledgers/{other}/business-documents/{body['id']}"
    assert client.get(foreign_path).status_code == 404
    edit = deepcopy(body)
    del edit["id"]
    edit.update(expected_version=1, refresh_snapshots=False)
    edit["lines"][0]["quantity"] = "4.00"
    assert client.put(foreign_path, json=edit).status_code == 404
    changed = client.put(path, json=edit)
    assert changed.status_code == 200, changed.text
    current = changed.json()
    assert current["version"] == 2 and current["lines"][0]["version"] == 2
    assert current["lines"][0]["quantity"] == "4.00" and current["total_amount"] == "75.76"
    stale = client.put(path, json=edit)
    assert stale.status_code == 409 and stale.json()["detail"]["code"] == "version_conflict"
    assert client.get(path).json() == current
    invalid = deepcopy(edit)
    invalid["expected_version"] = 2
    invalid["lines"][0]["unit_price"] = "0.001"
    rejected = client.put(path, json=invalid)
    assert rejected.status_code == 422 and rejected.json()["detail"]["code"] == "amount_precision"
    assert client.get(path).json() == current
    archived = client.patch(path + "/archive", json=dict(expected_version=2, archived=True))
    assert archived.status_code == 200 and archived.json()["archived"]
    assert archived.json()["version"] == 3 and archived.json()["lines"] == current["lines"]
    assert client.get(base + "?include_archived=false").json() == []
    assert len(client.get(base).json()) == 1
    restored = client.patch(path + "/archive", json=dict(expected_version=3, archived=False))
    assert restored.status_code == 200 and restored.json()["version"] == 4
    assert not restored.json()["archived"] and restored.json()["lines"] == current["lines"]
    assert client.get(path).json() == restored.json()
    assert financial_counts(engine) == financial
