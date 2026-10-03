from uuid import UUID

import pytest
from coinpup_api.auth import AuthError, AuthService, Identity, cookie_name, create_auth_router
from coinpup_api.config import Settings
from coinpup_api.security import (
    csrf_token_for,
    hash_password,
    new_session_token,
    normalize_username,
    token_digest,
    valid_csrf_token,
    verify_password,
)
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy.exc import OperationalError

ORIGIN = "http://localhost:8000"
PASSWORD = "fictional-test-password-123"
IDENTITY = Identity(UUID("00000000-0000-0000-0000-000000000001"), "test-admin")


def client_for(settings=None):
    settings = settings or Settings(_env_file=None, environment="test")
    app = FastAPI()
    app.include_router(create_auth_router(settings, None))
    scheme = "https" if settings.environment == "production" else "http"
    return TestClient(app, base_url=f"{scheme}://localhost")


def test_password_hash_uses_argon2_and_rejects_wrong_password():
    encoded = hash_password(PASSWORD)
    assert encoded.startswith("$argon2id$")
    assert PASSWORD not in encoded
    assert verify_password(encoded, PASSWORD)
    assert not verify_password(encoded, "wrong-password")
    assert not verify_password("malformed", PASSWORD)
    with pytest.raises(ValueError):
        hash_password("short")


def test_username_and_session_credentials():
    assert normalize_username("  Test-Admin  ") == "test-admin"
    with pytest.raises(ValueError):
        normalize_username("not a username")
    first, second = new_session_token(), new_session_token()
    assert first != second
    assert len(token_digest(first)) == 64
    csrf = csrf_token_for(first)
    assert csrf != token_digest(first)
    assert first not in csrf
    assert valid_csrf_token(first, csrf)
    assert not valid_csrf_token(first, csrf_token_for(second))
    assert not valid_csrf_token(first, None)
    assert not valid_csrf_token(first, "é" * 64)


@pytest.mark.parametrize("origin", [None, "null", "https://attacker.example", ORIGIN + ".attacker"])
def test_write_requires_allowed_origin_before_accessing_database(origin):
    with client_for() as client:
        headers = {"origin": origin} if origin else {}
        response = client.post(
            "/api/v1/auth/login", json={"username": "admin", "password": PASSWORD}, headers=headers
        )
        assert response.status_code == 403
        assert response.json()["detail"] == "origin_not_allowed"
        assert response.headers["cache-control"] == "no-store"
        assert client.post("/api/v1/auth/logout", headers=headers).status_code == 403


def test_no_session_is_unauthorized_and_missing_database_is_unavailable():
    with client_for() as client:
        assert client.get("/api/v1/auth/session").status_code == 401
        response = client.post(
            "/api/v1/auth/login",
            headers={"origin": ORIGIN},
            json={"username": "admin", "password": PASSWORD},
        )
        assert response.status_code == 503


@pytest.mark.parametrize("production", [False, True])
def test_cookie_session_csrf_and_logout(monkeypatch, production):
    token = new_session_token()
    revoked = []
    config = Settings(
        _env_file=None,
        environment="production" if production else "test",
        allowed_origins=["https://localhost"] if production else [ORIGIN],
    )
    origin = config.allowed_origins[0]

    def get_session(self, supplied):
        if supplied != token or revoked:
            raise AuthError("authentication_required", 401)
        return IDENTITY

    monkeypatch.setattr(AuthService, "login", lambda *args: (IDENTITY, token))
    monkeypatch.setattr(AuthService, "get_session", get_session)
    monkeypatch.setattr(AuthService, "logout", lambda self, token: revoked.append(token))
    with client_for(config) as client:
        response = client.post(
            "/api/v1/auth/login",
            headers={"origin": origin},
            json={"username": "test-admin", "password": PASSWORD},
        )
        assert response.status_code == 200
        assert response.json()["user"]["username"] == "test-admin"
        assert token not in response.text
        cookie = response.headers["set-cookie"]
        assert "HttpOnly" in cookie and "SameSite=strict" in cookie and "Path=/" in cookie
        assert ("; Secure" in cookie) is production
        assert cookie_name(config) in cookie
        csrf = response.json()["csrf_token"]
        assert client.get("/api/v1/auth/session").json()["csrf_token"] == csrf
        assert client.post("/api/v1/auth/logout", headers={"origin": origin}).status_code == 403
        assert (
            client.post(
                "/api/v1/auth/logout", headers={"origin": origin, "x-csrf-token": "wrong"}
            ).status_code
            == 403
        )
        # An authenticated login is also a write and requires its existing CSRF token.
        assert (
            client.post(
                "/api/v1/auth/login",
                headers={"origin": origin},
                json={"username": "test-admin", "password": PASSWORD},
            ).status_code
            == 403
        )
        response = client.post(
            "/api/v1/auth/logout", headers={"origin": origin, "x-csrf-token": csrf}
        )
        assert response.status_code == 204
        assert response.content == b""
        assert revoked == [token]
        assert client.get("/api/v1/auth/session").status_code == 401


def test_rate_limit_returns_retry_after(monkeypatch):
    def throttled(*args):
        raise AuthError("login_rate_limited", 429, 30)

    monkeypatch.setattr(AuthService, "login", throttled)
    with client_for() as client:
        response = client.post(
            "/api/v1/auth/login",
            headers={"origin": ORIGIN},
            json={"username": "test-admin", "password": PASSWORD},
        )
        assert response.status_code == 429
        assert response.headers["retry-after"] == "30"


def test_auth_database_failure_does_not_echo_exception(monkeypatch, caplog):
    def unavailable(*args):
        raise OperationalError("private statement", {}, Exception("fictional-private-db-password"))

    monkeypatch.setattr(AuthService, "login", unavailable)
    with client_for() as client:
        response = client.post(
            "/api/v1/auth/login",
            headers={"origin": ORIGIN},
            json={"username": "test-admin", "password": PASSWORD},
        )
        assert response.status_code == 503
        assert "fictional-private-db-password" not in response.text + caplog.text
        assert "private statement" not in response.text + caplog.text


def test_production_origin_configuration_is_explicit_and_https():
    with pytest.raises(ValidationError):
        Settings(_env_file=None, environment="production")
    with pytest.raises(ValidationError):
        Settings(_env_file=None, environment="production", allowed_origins=[ORIGIN])
    for origin in ["*", "https://example.com/path", "https://user:secret@example.com", "null"]:
        with pytest.raises(ValidationError):
            Settings(_env_file=None, allowed_origins=[origin])
    settings = Settings(
        _env_file=None, environment="production", allowed_origins=["https://example.com:443"]
    )
    assert settings.allowed_origins == ["https://example.com"]
