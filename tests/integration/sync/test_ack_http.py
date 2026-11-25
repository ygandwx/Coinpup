from copy import deepcopy
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from coinpup_api.sync.ack_models import PageAcknowledgement as Ack
from sqlalchemy import select

from tests.integration.sync.test_change_log_database import change_setup as change_setup
from tests.integration.sync.test_change_log_database import log_rows

pytestmark = pytest.mark.integration


def test_complete_session_page_old_receipt_and_current_cursor(authenticated_client, change_setup):
    client, engine, owner = authenticated_client
    assert change_setup["owner"] == owner
    device = uuid4()
    root = f"/api/v1/sync/devices/{device}"
    url = root + "/acknowledgements"
    latest = url + "/latest"
    upper = client.get("/api/v1/changes/start").json()["cursor"]

    def page(prior=None):
        after = prior["seq"] if prior else "0"
        bounds = {"after": after, "through": upper, "limit": 1}
        received = client.get("/api/v1/changes", params=bounds)
        assert received.status_code == 200
        return {**bounds, "prior": prior, "received": received.json()}

    def state():
        with engine.connect() as db:
            return db.execute(select(Ack.__table__).order_by(Ack.last_seq)).all(), log_rows(engine)

    before = state()
    first = page()
    assert client.get(latest).status_code == client.post(url, json=first).status_code == 404
    assert client.put(root).status_code == 200
    assert client.get(latest).status_code == 200 and client.get(latest).json() is None
    for headers in [{"x-csrf-token": "fictional-invalid"}, {"origin": "https://example.invalid"}]:
        assert client.post(url, json=first, headers=headers).status_code == 403
    notice = first["received"]["changes"][0]
    for patch in [
        {"after": 0},
        {"after": "00"},
        {"limit": True},
        {"limit": 201},
        {"owner_id": str(owner)},
        {"received": {**first["received"], "changes": [notice] * 201}},
        *[
            {"received": {**first["received"], "changes": [notice | fields]}}
            for fields in [
                {"changed_at": "2026-01-01T00:00:00"},
                {"changed_at": 1},
                {"changed_at": "1"},
                {"changed_at": "0001-01-01T00:00:00+00:01"},
                {"changed_at": "9999-12-31T23:59:59-00:01"},
                {"extra": True},
            ]
        ],
    ]:
        invalid = client.post(url, json=first | patch)
        assert invalid.status_code == 422
        assert all("input" not in detail for detail in invalid.json()["detail"])
    assert state() == before

    r = client.post(url, json=first)
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    old = r.json()
    assert (old["owner_id"], old["device_id"], old["cursor"], old["count"], old["limit"]) == (
        str(owner),
        str(device),
        notice["seq"],
        1,
        1,
    )
    second = page(notice)
    bad = deepcopy(second)
    bad["prior"]["entity_id"] = str(uuid4())
    assert client.post(url, json=bad).status_code == 409
    invalid = deepcopy(second)
    invalid["prior"]["changed_at"] = "0001-01-01T00:00:00+00:01"
    assert client.post(url, json=invalid).status_code == 422
    same_instant = datetime.fromisoformat(notice["changed_at"]).astimezone(
        timezone(timedelta(hours=5, minutes=30))
    )
    second["prior"] = notice | {"changed_at": same_instant.isoformat()}
    r = client.post(url, json=second)
    assert r.status_code == 200
    current = r.json()
    assert (
        current["after"] == old["cursor"] and current["cursor"] == second["received"]["next_cursor"]
    )
    assert client.put(root + "/retirement").status_code == 200
    assert client.post(url, json=first).json() == old
    assert client.get(latest).json() == current
    bad = deepcopy(first)
    bad["received"]["changes"][0]["entity_version"] += 1
    assert client.post(url, json=bad).status_code == 409
    assert client.post(url, json=page(second["received"]["changes"][0])).status_code == 409
    after = state()
    assert len(after[0]) == len(before[0]) + 2 and after[1] == before[1]
    client.cookies.clear()
    assert client.post(url, json=first).status_code == client.get(latest).status_code == 401
    assert state() == after
