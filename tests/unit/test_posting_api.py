"""Financial HTTP boundaries must preserve exact commands and session ownership."""

from uuid import UUID

import pytest
from coinpup_api.auth import AuthError, AuthService, Identity
from coinpup_api.config import Settings
from coinpup_api.ledger.posting import PostingService
from coinpup_api.ledger.service import LedgerError
from coinpup_api.main import create_app
from coinpup_api.security import csrf_token_for
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError, OperationalError

ORIGIN = "http://localhost:8000"
TOKEN = "fictional-posting-unit-session"
OWNER = UUID("00000000-0000-0000-0000-000000000001")
RECORD = "00000000-0000-0000-0000-000000000002"
LEDGER = f"/api/v1/ledgers/{RECORD}"
BASE = {
    "account_id": RECORD,
    "asset_id": "USD",
    "amount": "10.00",
    "transaction_date": "2026-10-03",
}
CLASSIFIED = BASE | {
    "recognition_date": "2026-09-30",
    "splits": [{"category_id": RECORD, "amount": "10.00"}],
}
WRITES = [
    ("/opening-balances", BASE, "post_opening"),
    ("/income", CLASSIFIED, "post_income"),
    ("/expenses", CLASSIFIED, "post_expense"),
]


class Probe:
    def check(self):
        pass

    def close(self):
        pass


@pytest.fixture
def client():
    with TestClient(create_app(Settings(_env_file=None, environment="test"), Probe())) as client:
        yield client


@pytest.fixture
def signed_in(client, monkeypatch):
    def resolve(self, token):
        if token != TOKEN:
            raise AuthError("authentication_required", 401)
        return Identity(OWNER, "fictional-admin")

    monkeypatch.setattr(AuthService, "get_session", resolve)
    client.cookies.set("coinpup_session", TOKEN)
    client.headers.update(
        {"origin": ORIGIN, "x-csrf-token": csrf_token_for(TOKEN), "idempotency-key": "test-command"}
    )
    return client


@pytest.mark.parametrize("suffix", ["/operations", f"/operations/{RECORD}", "/balances"])
def test_financial_reads_require_session(client, suffix):
    response = client.get(LEDGER + suffix)
    assert response.status_code == 401
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("suffix,body,method", WRITES)
def test_financial_writes_guard_origin_session_and_csrf(
    signed_in, suffix, body, method, monkeypatch
):
    def forbidden(*args, **kwargs):
        pytest.fail("Unauthorized financial service call")

    monkeypatch.setattr(PostingService, method, forbidden)
    for origin in ("null", "https://attacker.example"):
        assert (
            signed_in.post(LEDGER + suffix, json=body, headers={"origin": origin}).status_code
            == 403
        )
    signed_in.headers.pop("origin")
    assert signed_in.post(LEDGER + suffix, json=body).status_code == 403
    signed_in.headers["origin"] = ORIGIN
    signed_in.headers.pop("x-csrf-token")
    assert signed_in.post(LEDGER + suffix, json=body).status_code == 403
    signed_in.headers["x-csrf-token"] = "wrong"
    assert signed_in.post(LEDGER + suffix, json=body).status_code == 403
    signed_in.cookies.clear()
    assert signed_in.post(LEDGER + suffix, json=body).status_code == 401


@pytest.mark.parametrize("suffix,body,method", WRITES)
def test_financial_command_forwards_exact_body_owner_and_key(
    signed_in, suffix, body, method, monkeypatch
):
    calls = []

    def record(self, owner_id, ledger_id, payload, key):
        calls.append((owner_id, ledger_id, payload.model_dump(mode="json"), key))
        raise LedgerError("synthetic_conflict", 409, "Synthetic conflict")

    monkeypatch.setattr(PostingService, method, record)
    response = signed_in.post(LEDGER + suffix, json=body)
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "synthetic_conflict"
    assert len(calls) == 1
    owner_id, ledger_id, payload, key = calls[0]
    assert (owner_id, ledger_id, key) == (OWNER, UUID(RECORD), "test-command")
    assert payload["amount"] == "10.00"
    assert payload["transaction_date"] == "2026-10-03"
    if suffix != "/opening-balances":
        assert payload["recognition_date"] == "2026-09-30"
        assert payload["splits"] == body["splits"]


@pytest.mark.parametrize("key", [None, "", "contains space", "a" * 129])
def test_missing_or_invalid_idempotency_key_never_posts(signed_in, monkeypatch, key):
    def forbidden(*args, **kwargs):
        pytest.fail("Invalid command reached service")

    monkeypatch.setattr(PostingService, "post_opening", forbidden)
    signed_in.headers.pop("idempotency-key")
    if key is not None:
        signed_in.headers["idempotency-key"] = key
    assert signed_in.post(LEDGER + "/opening-balances", json=BASE).status_code == 422


@pytest.mark.parametrize(
    "extra",
    [{"amount": 10.0}, {"amount": "1e2"}, {"owner_id": str(OWNER)}, {"transaction_date": 0}],
)
def test_http_rejects_float_coercion_and_client_ownership(signed_in, monkeypatch, extra):
    def forbidden(*args, **kwargs):
        pytest.fail("Invalid command reached service")

    monkeypatch.setattr(PostingService, "post_opening", forbidden)
    assert signed_in.post(LEDGER + "/opening-balances", json=BASE | extra).status_code == 422


def test_financial_reads_forward_bounded_query_and_owner(signed_in, monkeypatch):
    calls = []

    def balances(self, owner_id, ledger_id, **kwargs):
        calls.append((owner_id, ledger_id, kwargs))
        return []

    monkeypatch.setattr(PostingService, "balances", balances)
    assert signed_in.get(LEDGER + f"/balances?account_id={RECORD}&limit=2&offset=3").json() == []
    assert calls == [(OWNER, UUID(RECORD), {"account_id": UUID(RECORD), "limit": 2, "offset": 3})]
    for suffix in ("/operations", "/balances"):
        for query in ("limit=0", "limit=201", "offset=-1", "offset=100001"):
            assert signed_in.get(LEDGER + suffix + "?" + query).status_code == 422
    assert len(calls) == 1


@pytest.mark.parametrize("error_type,status", [(IntegrityError, 409), (OperationalError, 503)])
def test_database_errors_do_not_disclose_financial_values(
    signed_in, monkeypatch, caplog, error_type, status
):
    secret = "fictional-private-account-description"

    def unavailable(*args, **kwargs):
        raise error_type("private financial SQL", {"description": secret}, Exception(secret))

    monkeypatch.setattr(PostingService, "post_opening", unavailable)
    response = signed_in.post(LEDGER + "/opening-balances", json=BASE)
    assert response.status_code == status
    assert secret not in response.text + caplog.text
    assert "private financial SQL" not in response.text + caplog.text


def test_every_financial_write_is_guarded_and_documents_command_key(client):
    paths = client.get("/openapi.json").json()["paths"]
    writes = {
        path: specification
        for path, methods in paths.items()
        for method, specification in methods.items()
        if method in {"post", "patch", "put", "delete"}
        and "financial operations" in specification.get("tags", [])
    }
    assert set(writes) == {"/api/v1/ledgers/{ledger_id}" + suffix for suffix, _, _ in WRITES}
    for specification in writes.values():
        headers = {
            item["name"]: item for item in specification["parameters"] if item["in"] == "header"
        }
        assert headers["Idempotency-Key"]["required"] is True
        assert "X-CSRF-Token" in headers
