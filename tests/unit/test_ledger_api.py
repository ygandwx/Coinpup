"""Every structure route must use the server session identity and guarded writes."""

from uuid import UUID

import pytest
from coinpup_api.auth import AuthError, AuthService, Identity
from coinpup_api.config import Settings
from coinpup_api.ledger.service import LedgerError, LedgerService
from coinpup_api.main import create_app
from coinpup_api.security import csrf_token_for
from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError

ORIGIN = "http://localhost:8000"
TOKEN = "fictional-unit-session-token"
OWNER = UUID("00000000-0000-0000-0000-000000000001")
RECORD = "00000000-0000-0000-0000-000000000002"
LEDGER = f"/api/v1/ledgers/{RECORD}"
WRITES = [
    ("POST", "/api/v1/entities", {"name": "Fixture", "kind": "personal", "base_asset_id": "USD"}),
    ("PATCH", f"/api/v1/entities/{RECORD}", {"expected_version": 1, "name": "Renamed"}),
    ("POST", "/api/v1/assets", {"code": "JPY", "kind": "fiat", "scale": 0}),
    ("PATCH", "/api/v1/assets/USD", {"expected_version": 1, "enabled": False}),
    (
        "POST",
        LEDGER + "/accounts",
        {"name": "Fixture", "kind": "wise", "asset_ids": ["USD", "EUR"]},
    ),
    ("PATCH", LEDGER + f"/accounts/{RECORD}", {"expected_version": 1, "archived": True}),
    ("POST", LEDGER + "/categories", {"name": "Fixture", "kind": "expense"}),
    ("PATCH", LEDGER + f"/categories/{RECORD}", {"expected_version": 1, "name": "Renamed"}),
]
READS = [
    "/api/v1/assets",
    "/api/v1/category-templates",
    "/api/v1/entities",
    f"/api/v1/entities/{RECORD}",
    LEDGER,
    LEDGER + "/accounts",
    LEDGER + "/categories",
]


class Probe:
    def check(self):
        pass

    def close(self):
        pass


@pytest.fixture
def client():
    app = create_app(Settings(_env_file=None, environment="test"), Probe())
    with TestClient(app) as client:
        yield client


@pytest.fixture
def signed_in(client, monkeypatch):
    def resolve(self, token):
        if token != TOKEN:
            raise AuthError("authentication_required", 401)
        return Identity(OWNER, "fictional-admin")

    monkeypatch.setattr(AuthService, "get_session", resolve)
    client.cookies.set("coinpup_session", TOKEN)
    return client


@pytest.mark.parametrize("path", READS)
def test_every_structure_read_requires_a_session(client, path):
    response = client.get(path)
    assert response.status_code == 401
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("method,path,body", WRITES)
def test_every_structure_write_requires_origin_session_and_csrf(signed_in, method, path, body):
    for origin in (None, "null", "https://attacker.example"):
        headers = {"x-csrf-token": csrf_token_for(TOKEN)}
        if origin is not None:
            headers["origin"] = origin
        response = signed_in.request(method, path, json=body, headers=headers)
        assert response.status_code == 403
        assert response.json()["detail"] == "origin_not_allowed"
    for csrf in (None, "wrong"):
        headers = {"origin": ORIGIN}
        if csrf is not None:
            headers["x-csrf-token"] = csrf
        response = signed_in.request(method, path, json=body, headers=headers)
        assert response.status_code == 403
        assert response.json()["detail"] == "csrf_failed"
    signed_in.cookies.clear()
    assert signed_in.request(method, path, json=body, headers={"origin": ORIGIN}).status_code == 401


def test_reads_forward_session_owner_and_bounded_pagination(signed_in, monkeypatch):
    calls = []

    def list_entities(self, owner_id, **kwargs):
        calls.append((owner_id, kwargs))
        return []

    monkeypatch.setattr(LedgerService, "list_entities", list_entities)
    response = signed_in.get("/api/v1/entities?limit=2&offset=3&include_archived=true")
    assert response.status_code == 200 and response.json() == []
    assert calls == [(OWNER, {"include_archived": True, "limit": 2, "offset": 3})]
    for query in ("limit=0", "limit=201", "offset=-1", "offset=100001"):
        assert signed_in.get("/api/v1/entities?" + query).status_code == 422
    assert len(calls) == 1


def test_valid_write_uses_session_identity_and_stable_domain_error(signed_in, monkeypatch):
    calls = []

    def create_entity(self, owner_id, body):
        calls.append((owner_id, body.name))
        raise LedgerError("test_conflict", 409, "Synthetic conflict")

    monkeypatch.setattr(LedgerService, "create_entity", create_entity)
    response = signed_in.post(
        "/api/v1/entities",
        json=WRITES[0][2],
        headers={"origin": ORIGIN, "x-csrf-token": csrf_token_for(TOKEN)},
    )
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "test_conflict"
    assert calls == [(OWNER, "Fixture")]


def test_expired_or_revoked_identity_never_reaches_service(signed_in, monkeypatch):
    def reject(*args):
        raise AuthError("authentication_required", 401)

    def forbidden(*args, **kwargs):
        pytest.fail("Service ran with an invalid session")

    monkeypatch.setattr(AuthService, "get_session", reject)
    monkeypatch.setattr(LedgerService, "list_entities", forbidden)
    assert signed_in.get("/api/v1/entities").status_code == 401


def test_rejected_inputs_and_database_failures_do_not_echo_values(signed_in, monkeypatch, caplog):
    headers = {"origin": ORIGIN, "x-csrf-token": csrf_token_for(TOKEN)}
    secret = "fictional-private-bank-data"
    response = signed_in.post(
        "/api/v1/entities", json=WRITES[0][2] | {"owner_id": secret}, headers=headers
    )
    assert response.status_code == 422
    assert secret not in response.text

    def unavailable(*args, **kwargs):
        raise OperationalError("private statement", {}, Exception(secret))

    monkeypatch.setattr(LedgerService, "list_entities", unavailable)
    response = signed_in.get("/api/v1/entities")
    assert response.status_code == 503
    assert secret not in response.text + caplog.text
    assert "private statement" not in response.text + caplog.text


def test_all_registered_unsafe_structure_routes_are_covered(client):
    documented = client.get("/openapi.json").json()["paths"]
    unsafe = {
        (method.upper(), path)
        for path, methods in documented.items()
        for method, specification in methods.items()
        if "ledger structure" in specification.get("tags", [])
        if method in {"post", "patch", "put", "delete"}
    }
    assert unsafe == {
        ("POST", "/api/v1/assets"),
        ("PATCH", "/api/v1/assets/{asset_id}"),
        ("POST", "/api/v1/entities"),
        ("PATCH", "/api/v1/entities/{entity_id}"),
        ("POST", "/api/v1/ledgers/{ledger_id}/accounts"),
        ("PATCH", "/api/v1/ledgers/{ledger_id}/accounts/{account_id}"),
        ("POST", "/api/v1/ledgers/{ledger_id}/categories"),
        ("PATCH", "/api/v1/ledgers/{ledger_id}/categories/{category_id}"),
    }
