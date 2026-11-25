"""Device authorization ties the first and subsequent checkpoints to complete owned notices."""

from uuid import uuid4

import pytest
from coinpup_api.ledger.schemas import EntityCreate

from tests.integration.sync.test_change_log_database import change_setup as change_setup

pytestmark = pytest.mark.integration


def test_http_registration_binds_bootstrap_with_no_initial_incremental_page(
    change_setup, authenticated_client
):
    s = change_setup
    client, _, _ = authenticated_client
    starts = "/api/v1/changes/start"
    changes = "/api/v1/changes"
    saved = client.get(starts).json()
    assert saved["witness"]["seq"] == saved["cursor"] != "0"
    device = f"/api/v1/sync/devices/{uuid4()}"
    fork = saved | {"witness": saved["witness"] | {"entity_id": "independent-fork"}}
    assert client.put(device, json=fork).status_code == 409
    for timestamp in ("1", "0001-01-01T00:00:00+00:01", "9999-12-31T23:59:59-00:01"):
        invalid = saved | {"witness": saved["witness"] | {"changed_at": timestamp}}
        result = client.put(device, json=invalid)
        assert result.status_code == 422
        assert all("input" not in detail for detail in result.json()["detail"])
    assert client.get(device + "/checkpoint").status_code == 404
    assert client.put(device, json=saved).json()["start"] == saved
    assert client.get(device + "/checkpoint").json() == saved
    assert client.get(device + "/acknowledgements/latest").json() is None

    earlier = client.get(changes, params={"through": saved["cursor"]}).json()
    outside = {
        "after": "0",
        "through": saved["cursor"],
        "limit": 100,
        "prior": None,
        "received": earlier,
    }
    write = device + "/acknowledgements"
    result = client.post(write, json=outside)
    assert (
        result.status_code == 409 and result.json()["detail"]["code"] == "sync_checkpoint_conflict"
    )
    assert client.get(device + "/checkpoint").json() == saved

    s["structure"].create_entity(
        s["owner"],
        EntityCreate(kind="personal", name="Fictional bootstrap peer", base_asset_id="USD"),
    )
    end = client.get(starts).json()
    page = client.get(changes, params={"after": saved["cursor"], "through": end["cursor"]}).json()
    body = {
        "after": saved["cursor"],
        "through": end["cursor"],
        "limit": 100,
        "prior": saved["witness"],
        "received": page,
    }
    result = client.post(write, json=body)
    assert result.status_code == 200, result.json()
    assert client.post(write, json=body).content == result.content
    assert client.get(device + "/checkpoint").json() == end
    assert client.put(device, json=end).status_code == 409
    assert client.put(device + "/retirement").json()["start"] == saved
    assert client.post(write, json=body).content == result.content
    assert client.get(device + "/checkpoint").json() == end
