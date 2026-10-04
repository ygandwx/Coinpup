"""The new notification endpoint preserves the existing session and error boundaries."""

from uuid import UUID

import pytest
from coinpup_api.auth import AuthError, AuthService, Identity
from coinpup_api.config import Settings
from coinpup_api.ledger.service import LedgerError
from coinpup_api.main import create_app
from coinpup_api.sync.schemas import MAX_CURSOR, ChangePage
from coinpup_api.sync.service import ChangeService
from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError

OWNER = UUID("00000000-0000-4000-8000-000000000001")
TOKEN = "fictional-change-read-session"
PATH = "/api/v1/changes"


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
    return client


def test_changes_require_session_and_never_allow_http_writes(client, monkeypatch):
    monkeypatch.setattr(
        ChangeService, "list_changes", lambda *args, **kwargs: pytest.fail("Anonymous read")
    )
    response = client.get(PATH)
    assert response.status_code == 401 and response.headers["cache-control"] == "no-store"
    for method in ("POST", "PUT", "PATCH", "DELETE"):
        assert client.request(method, PATH).status_code == 405


@pytest.mark.parametrize(
    "after,limit", [(None, None), ("9007199254740993", 2), (str(MAX_CURSOR), 200)]
)
def test_changes_forward_cookie_owner_exact_cursor_and_bounded_limit(
    signed_in, monkeypatch, after, limit
):
    calls = []

    def records(self, owner_id, **options):
        calls.append((owner_id, options))
        return ChangePage(changes=[], next_cursor=options["after"])

    monkeypatch.setattr(ChangeService, "list_changes", records)
    params = {"owner_id": "00000000-0000-4000-8000-000000000099"}
    if after is not None:
        params["after"] = after
    if limit is not None:
        params["limit"] = limit
    response = signed_in.get(PATH, params=params)
    expected = after if after is not None else "0"
    assert response.status_code == 200
    assert response.json() == {"changes": [], "next_cursor": expected}
    assert calls == [(OWNER, {"after": expected, "limit": limit if limit is not None else 100})]
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize(
    "invalid",
    [
        "",
        "00",
        "01",
        "-1",
        "+1",
        "1.0",
        "1e2",
        "1\n",
        "2026-01-01",
        "1 OR 1=1",
        "١",
        str(MAX_CURSOR + 1),
        "9" * 100,
    ],
)
def test_http_rejects_noncanonical_or_overflow_cursor_before_service(
    signed_in, monkeypatch, invalid
):
    def forbidden(*args, **kwargs):
        pytest.fail("Invalid cursor reached service")

    monkeypatch.setattr(ChangeService, "list_changes", forbidden)
    response = signed_in.get(PATH, params={"after": invalid})
    assert response.status_code == 422
    assert response.json()["detail"][0]["loc"] == ["query", "after"]
    assert set(response.json()["detail"][0]) == {"loc", "type", "msg"}


@pytest.mark.parametrize("limit", [0, 201, -1, "true", "1.5"])
def test_http_rejects_invalid_page_limit_before_service(signed_in, monkeypatch, limit):
    def forbidden(*args, **kwargs):
        pytest.fail("Invalid page reached service")

    monkeypatch.setattr(ChangeService, "list_changes", forbidden)
    response = signed_in.get(PATH, params={"limit": limit})
    assert response.status_code == 422
    assert response.json()["detail"][0]["loc"] == ["query", "limit"]


def test_changes_translate_existing_domain_error_contract(signed_in, monkeypatch):
    def missing(*args, **kwargs):
        raise LedgerError("not_found", 404, "The requested record was not found.")

    monkeypatch.setattr(ChangeService, "list_changes", missing)
    response = signed_in.get(PATH)
    assert response.status_code == 404
    assert response.json() == {
        "detail": {"code": "not_found", "message": "The requested record was not found."}
    }


def test_changes_use_sanitized_database_error_boundary(signed_in, monkeypatch, caplog):
    secret = "fictional-private-change-notification"

    def unavailable(*args, **kwargs):
        raise OperationalError("private notification SQL", {"entity_id": secret}, Exception(secret))

    monkeypatch.setattr(ChangeService, "list_changes", unavailable)
    response = signed_in.get(PATH)
    assert response.status_code == 503 and response.json() == {"detail": "Service unavailable"}
    assert secret not in response.text + caplog.text
    assert "private notification SQL" not in response.text + caplog.text
