"""Real HTTP/database master-data responses, conflicts and same-owner ledger isolation."""

from uuid import uuid4

import pytest
from coinpup_api.ledger.schemas import EntityCreate
from coinpup_api.ledger.service import LedgerService
from coinpup_api.sync.service import ChangeService

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("kind", ["parties", "projects"])
def test_master_data_http_lifecycle_and_duplicate_identity(authenticated_client, kind):
    client, engine, owner = authenticated_client
    structure = LedgerService(engine)
    ledger, other = [
        structure.create_entity(
            owner,
            EntityCreate(kind="personal", name=f"Fictional HTTP {index}", base_asset_id="USD"),
        ).ledger.id
        for index in range(2)
    ]
    base = f"/api/v1/ledgers/{ledger}/business-{kind}"
    identifier = str(uuid4())
    body = dict(id=identifier, name="Fictional 中文资料", notes="Fictional notes")
    if kind == "parties":
        body["role"] = "supplier"
    result = client.post(base, json=body)
    assert result.status_code == 201, result.text
    original = result.json()
    assert original["id"] == identifier and original["ledger_id"] == str(ledger)
    assert original["version"] == 1 and original["name"] == body["name"]
    assert result.headers["cache-control"] == "no-store"
    duplicate = client.post(base, json=body)
    assert duplicate.status_code == 409 and duplicate.json()["detail"]["code"] == "duplicate_record"
    assert client.get(base + "/" + identifier).json() == original
    assert client.get(base).json() == [original]
    assert client.get(f"/api/v1/ledgers/{other}/business-{kind}/{identifier}").status_code == 404
    assert client.get(f"/api/v1/ledgers/{other}/business-{kind}").json() == []
    update = dict(expected_version=1, name="Fictional changed", notes=None, archived=True)
    changed = client.patch(base + "/" + identifier, json=update)
    assert changed.status_code == 200 and changed.json()["version"] == 2
    assert changed.json()["notes"] is None and changed.json()["archived"]
    assert client.get(base + "?include_archived=false").json() == []
    stale = client.patch(base + "/" + identifier, json=update)
    assert stale.status_code == 409 and stale.json()["detail"]["code"] == "version_conflict"
    assert client.get(base + "/" + identifier).json() == changed.json()
    restored = client.patch(base + "/" + identifier, json=dict(expected_version=2, archived=False))
    assert restored.status_code == 200 and restored.json()["version"] == 3
    events = [
        row
        for row in ChangeService(engine).list_changes(owner, limit=200).changes
        if row.entity_id == identifier
    ]
    assert [(row.entity_type, row.entity_version, row.change_kind) for row in events] == [
        ("business_" + kind, 1, "upsert"),
        ("business_" + kind, 2, "archive"),
        ("business_" + kind, 3, "restore"),
    ]
