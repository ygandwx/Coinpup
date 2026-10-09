"""Every master data route keeps the authenticated owner and browser write boundary."""

from uuid import UUID

import pytest
from coinpup_api.business.service import BusinessService
from coinpup_api.ledger.service import LedgerError
from coinpup_api.security import csrf_token_for

from tests.unit.ledger.test_ledger_api import LEDGER, ORIGIN, OWNER, RECORD, TOKEN
from tests.unit.ledger.test_ledger_api import client as client
from tests.unit.ledger.test_ledger_api import signed_in as signed_in

READS = [
    LEDGER + suffix
    for suffix in (
        "/business-parties",
        f"/business-parties/{RECORD}",
        "/business-projects",
        f"/business-projects/{RECORD}",
    )
]
WRITES = [
    (
        "POST",
        LEDGER + "/business-parties",
        dict(id=RECORD, name="Fictional party", role="customer"),
    ),
    ("PATCH", LEDGER + f"/business-parties/{RECORD}", dict(expected_version=1, archived=True)),
    ("POST", LEDGER + "/business-projects", dict(id=RECORD, name="Fictional project")),
    ("PATCH", LEDGER + f"/business-projects/{RECORD}", dict(expected_version=1, archived=True)),
]


@pytest.mark.parametrize("path", READS)
def test_business_reads_require_session_and_disable_caching(client, path):
    response = client.get(path)
    assert response.status_code == 401 and response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("method,path,body", WRITES)
def test_business_writes_require_origin_csrf_and_session(signed_in, method, path, body):
    for origin in (None, "null", "https://attacker.example"):
        headers = {"x-csrf-token": csrf_token_for(TOKEN)}
        if origin is not None:
            headers["origin"] = origin
        response = signed_in.request(method, path, json=body, headers=headers)
        assert response.status_code == 403 and response.json()["detail"] == "origin_not_allowed"
    for csrf in (None, "wrong"):
        headers = {"origin": ORIGIN}
        if csrf is not None:
            headers["x-csrf-token"] = csrf
        response = signed_in.request(method, path, json=body, headers=headers)
        assert response.status_code == 403 and response.json()["detail"] == "csrf_failed"
    signed_in.cookies.clear()
    assert signed_in.request(method, path, json=body, headers={"origin": ORIGIN}).status_code == 401


@pytest.mark.parametrize("kind", ["parties", "projects"])
def test_business_lists_use_session_owner_and_bounded_page(signed_in, monkeypatch, kind):
    calls = []

    def capture(self, owner, ledger, **options):
        calls.append((owner, ledger, options))
        return []

    monkeypatch.setattr(BusinessService, "list_" + kind, capture)
    url = LEDGER + "/business-" + kind
    result = signed_in.get(url + "?include_archived=false&limit=2&offset=3")
    assert result.status_code == 200 and result.json() == []
    assert calls == [(OWNER, UUID(RECORD), dict(include_archived=False, limit=2, offset=3))]
    for query in ("limit=0", "limit=201", "offset=-1", "offset=100001"):
        assert signed_in.get(url + "?" + query).status_code == 422
    assert len(calls) == 1


@pytest.mark.parametrize("method,path,body", WRITES)
def test_business_writes_forward_owned_identity_and_stable_errors(
    signed_in, monkeypatch, method, path, body
):
    calls = []
    action = ("create_" if method == "POST" else "update_") + (
        "party" if "parties" in path else "project"
    )

    def capture(self, owner, ledger, *args):
        calls.append((owner, ledger, args))
        raise LedgerError("version_conflict", 409, "The record changed; reload before editing.")

    monkeypatch.setattr(BusinessService, action, capture)
    result = signed_in.request(
        method, path, json=body, headers={"origin": ORIGIN, "x-csrf-token": csrf_token_for(TOKEN)}
    )
    assert result.status_code == 409 and result.json()["detail"]["code"] == "version_conflict"
    assert calls[0][:2] == (OWNER, UUID(RECORD))
    if method == "PATCH":
        assert calls[0][2][0] == UUID(RECORD)
    assert calls[0][2][-1].model_dump(mode="json", exclude_unset=True) == body


@pytest.mark.parametrize("kind, singular", [("parties", "party"), ("projects", "project")])
def test_business_gets_forward_both_path_ids(signed_in, monkeypatch, kind, singular):
    calls = []

    def capture(self, owner, ledger, identifier):
        calls.append((owner, ledger, identifier))
        raise LedgerError("not_found", 404, "The requested record was not found.")

    monkeypatch.setattr(BusinessService, "get_" + singular, capture)
    response = signed_in.get(LEDGER + f"/business-{kind}/{RECORD}")
    assert response.status_code == 404 and response.json()["detail"]["code"] == "not_found"
    assert calls == [(OWNER, UUID(RECORD), UUID(RECORD))]
