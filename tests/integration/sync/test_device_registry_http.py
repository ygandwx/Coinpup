from uuid import uuid4

import pytest

from tests.integration.sync.test_device_registry_storage import state

pytestmark = pytest.mark.integration


def test_session_owned_registry_original_history_and_no_device_authentication(authenticated_client):
    client, engine, owner = authenticated_client
    device = uuid4()
    path = f"/api/v1/sync/devices/{device}"
    scope = "/api/v1/sync/devices/{device_id}"
    api = client.app.openapi()["paths"]
    assert {p: set(api[scope + p]) for p in ("", "/retirement", "/history")} == {
        "": {"put"},
        "/retirement": {"put"},
        "/history": {"get"},
    }
    empty = state(engine)
    assert client.get(path + "/history").status_code == 404
    assert client.put(path + "/retirement").status_code == 404
    for headers in [{"x-csrf-token": "fictional-invalid"}, {"origin": "https://example.invalid"}]:
        assert client.put(path, headers=headers).status_code == 403
    assert state(engine) == empty
    first = client.put(path)
    assert first.status_code == 200 and first.headers["cache-control"] == "no-store"
    a = first.json()
    assert set(a) == {"owner_id", "device_id", "version", "state", "recorded_at", "start"}
    assert (a["owner_id"], a["device_id"], a["version"], a["state"]) == (
        str(owner),
        str(device),
        1,
        "registered",
    )
    assert a["start"] == {"cursor": "0", "witness": None}
    assert client.put(path).json() == a
    assert client.get(path + "/history").json() == {"revisions": [a]}
    assert (
        client.put(path + "/retirement", headers={"x-csrf-token": "fictional-invalid"}).status_code
        == 403
    )
    second = client.put(path + "/retirement")
    assert second.status_code == 200
    b = second.json()
    assert (b["owner_id"], b["device_id"], b["version"], b["state"]) == (
        str(owner),
        str(device),
        2,
        "retired",
    )
    assert client.put(path).json() == client.put(path + "/retirement").json() == b
    response = client.get(path + "/history")
    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    assert response.json() == {"revisions": [a, b]}
    completed = state(engine)
    assert len(completed[0]) == 2 and completed[1] == empty[1]
    assert client.put("/api/v1/sync/devices/not-a-uuid").status_code == 422
    client.cookies.clear()
    for method, url in [
        (client.get, path + "/history"),
        (client.put, path),
        (client.put, path + "/retirement"),
    ]:
        assert method(url).status_code == 401
    assert state(engine) == completed
