"""Run against the disposable migrated PostgreSQL database only."""

import os
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier

import pytest
from coinpup_api.admin import create_admin, reset_admin_password
from coinpup_api.auth import AuthError, AuthService, create_auth_router
from coinpup_api.config import Settings
from coinpup_api.database import Database
from coinpup_api.main import create_app
from coinpup_api.models import Administrator, AuthSession, LoginGuard
from coinpup_api.security import token_digest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import Session

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("COINPUP_RUN_DB_TESTS") != "1",
        reason="Requires an explicitly configured disposable PostgreSQL database",
    ),
]
PASSWORD = "fictional-integration-password-123"
ORIGIN = "http://localhost:8000"


@pytest.fixture
def database():
    config = Settings(environment="test", login_max_failures=3)
    database = Database(config)

    def cleanup():
        with database.engine.begin() as connection:
            connection.execute(delete(AuthSession))
            connection.execute(delete(Administrator))
            connection.execute(
                update(LoginGuard).values(
                    failure_count=0, window_started_at=None, locked_until=None
                )
            )

    cleanup()
    yield config, database
    cleanup()
    database.close()


def test_login_persisted_session_logout_and_expiry(database):
    config, db = database
    identifier = create_admin(db.engine, "test-admin", PASSWORD)
    app = FastAPI()
    app.include_router(create_auth_router(config, db.engine))
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/auth/login",
            headers={"origin": ORIGIN},
            json={"username": "test-admin", "password": PASSWORD},
        )
        assert response.status_code == 200
        assert response.json()["user"]["id"] == str(identifier)
        token = client.cookies["coinpup_session"]
        csrf = response.json()["csrf_token"]
        with Session(db.engine) as session:
            stored = session.get(AuthSession, token_digest(token))
            assert stored is not None
            assert stored.token_hash != token
        # A fresh service has no in-memory session state to rely on.
        assert AuthService(config, db.engine).get_session(token).id == identifier
        assert (
            client.post(
                "/api/v1/auth/logout", headers={"origin": ORIGIN, "x-csrf-token": csrf}
            ).status_code
            == 204
        )
        with pytest.raises(AuthError) as revoked:
            AuthService(config, db.engine).get_session(token)
        assert revoked.value.status == 401
        _, expired_token = AuthService(config, db.engine).login("test-admin", PASSWORD)
        with db.engine.begin() as connection:
            connection.execute(
                update(AuthSession)
                .where(AuthSession.token_hash == token_digest(expired_token))
                .values(expires_at=func.now() - timedelta(seconds=1))
            )
        with pytest.raises(AuthError) as expired:
            AuthService(config, db.engine).get_session(expired_token)
        assert expired.value.status == 401


def test_concurrent_initialization_creates_only_one_administrator(database):
    _, db = database

    def initialize(index):
        try:
            return create_admin(db.engine, f"test-admin-{index}", PASSWORD)
        except ValueError:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(initialize, [1, 2]))
    assert sum(result is not None for result in results) == 1
    with Session(db.engine) as session:
        assert session.scalar(select(func.count()).select_from(Administrator)) == 1


def test_failed_attempts_commit_and_are_shared_across_services(database):
    config, db = database
    create_admin(db.engine, "test-admin", PASSWORD)

    def attempt(index):
        try:
            # Distinct service instances exercise PostgreSQL serialization, not local locks.
            AuthService(config, db.engine).login("unknown" if index % 2 else "test-admin", "wrong")
        except AuthError as error:
            return error.status

    with ThreadPoolExecutor(max_workers=5) as pool:
        statuses = list(pool.map(attempt, range(5)))
    assert statuses.count(401) == 2
    assert statuses.count(429) == 3
    with Session(db.engine) as session:
        guard = session.get(LoginGuard, 1)
        assert guard.failure_count == 3
        assert guard.locked_until is not None
    with pytest.raises(AuthError) as locked:
        AuthService(config, db.engine).login("test-admin", PASSWORD)
    assert locked.value.status == 429
    # The lock expires without a process restart.
    with db.engine.begin() as connection:
        connection.execute(
            update(LoginGuard).values(locked_until=func.now() - timedelta(seconds=1))
        )
    assert AuthService(config, db.engine).login("test-admin", PASSWORD)[0].username == "test-admin"


def test_password_reset_revokes_every_session(database):
    config, db = database
    create_admin(db.engine, "test-admin", PASSWORD)
    service = AuthService(config, db.engine)
    tokens = [service.login("test-admin", PASSWORD)[1] for _ in range(2)]
    reset_admin_password(db.engine, "replacement-fictional-password-123")
    for token in tokens:
        with pytest.raises(AuthError):
            AuthService(config, db.engine).get_session(token)
    with pytest.raises(AuthError) as rejected:
        service.login("test-admin", PASSWORD)
    assert rejected.value.status == 401
    assert (
        service.login("test-admin", "replacement-fictional-password-123")[0].username
        == "test-admin"
    )


def test_password_change_checks_session_and_rotates_all_old_sessions(database):
    _, db = database
    origin = "https://coinpup.example"
    config = Settings(environment="production", allowed_origins=[origin], login_max_failures=5)
    create_admin(db.engine, "test-admin", PASSWORD)
    app = create_app(config, database=db)
    target = "/api/v1/auth/change-password"
    login_path = "/api/v1/auth/login"
    session_path = "/api/v1/auth/session"
    cookie_name = "__Host-coinpup_session"
    new_password = "fictional-changed-password-456"
    data = {
        "current_password": PASSWORD,
        "new_password": new_password,
        "confirm_password": new_password,
    }

    def login(client, password):
        return client.post(
            login_path,
            headers={"origin": origin},
            json={"username": "test-admin", "password": password},
        )

    with (
        TestClient(app, base_url=origin) as first,
        TestClient(app, base_url=origin) as second,
        TestClient(app, base_url=origin) as anonymous,
    ):
        old_csrf = login(first, PASSWORD).json()["csrf_token"]
        assert login(second, PASSWORD).status_code == 200
        old_cookie = first.cookies[cookie_name]
        headers = {"origin": origin, "x-csrf-token": old_csrf}
        assert anonymous.post(target, headers=headers, json=data).status_code == 401
        assert first.post(target, headers={"origin": origin}, json=data).status_code == 403
        assert (
            first.post(
                target, headers={**headers, "origin": "https://other.example"}, json=data
            ).status_code
            == 403
        )
        malformed = first.post(target, headers=headers, json={**data, "new_password": "secret123"})
        assert malformed.status_code == 422
        assert malformed.headers["cache-control"] == "no-store"
        assert "secret123" not in malformed.text and PASSWORD not in malformed.text
        mismatch = first.post(target, headers=headers, json={**data, "confirm_password": PASSWORD})
        assert mismatch.json()["detail"]["code"] == "password_confirmation_mismatch"
        wrong = first.post(
            target, headers=headers, json={**data, "current_password": "fictional-wrong-password"}
        )
        assert wrong.status_code == 400
        assert wrong.json()["detail"]["code"] == "incorrect_current_password"
        assert wrong.headers["cache-control"] == "no-store"
        assert "fictional" not in wrong.text
        assert first.cookies[cookie_name] == old_cookie
        assert second.get(session_path).status_code == 200
        result = first.post(target, headers=headers, json=data)
        assert result.status_code == 200
        new_csrf = result.json()["csrf_token"]
        assert new_csrf != old_csrf
        assert first.cookies[cookie_name] != old_cookie
        assert result.headers["cache-control"] == "no-store"
        assert all(
            flag in result.headers["set-cookie"]
            for flag in ("Secure", "HttpOnly", "SameSite=strict", "Path=/")
        )
        assert first.get(session_path).json()["csrf_token"] == new_csrf
        assert first.post(target, headers=headers, json=data).status_code == 403
        assert second.get(session_path).status_code == 401
        assert (
            anonymous.get(
                session_path, headers={"Cookie": f"{cookie_name}={old_cookie}"}
            ).status_code
            == 401
        )
        assert login(anonymous, PASSWORD).status_code == 401
        assert login(anonymous, new_password).status_code == 200


def test_password_change_shares_persistent_throttle_and_does_not_revoke_session_on_failure(
    database,
):
    config, db = database
    create_admin(db.engine, "test-admin", PASSWORD)
    service = AuthService(config, db.engine)
    _, current_token = service.login("test-admin", PASSWORD)
    for expected in (400, 400, 429):
        with pytest.raises(AuthError) as rejected:
            service.change_password(
                current_token,
                "fictional-wrong-password",
                "different-fictional",
                "different-fictional",
            )
        assert rejected.value.status == expected
    with pytest.raises(AuthError) as blocked:
        AuthService(config, db.engine).login("test-admin", PASSWORD)
    assert blocked.value.status == 429
    assert service.get_session(current_token).username == "test-admin"


def test_old_password_login_racing_change_cannot_leave_a_live_session(database):
    config, db = database
    create_admin(db.engine, "test-admin", PASSWORD)
    service = AuthService(config, db.engine)
    _, original = service.login("test-admin", PASSWORD)
    rendezvous = Barrier(2)
    replacement = "fictional-concurrent-new-password"

    def try_old_login():
        rendezvous.wait(timeout=5)
        try:
            return service.login("test-admin", PASSWORD)[1]
        except AuthError as error:
            assert error.status == 401
            return None

    with ThreadPoolExecutor(max_workers=1) as pool:
        competing = pool.submit(try_old_login)
        rendezvous.wait(timeout=5)
        _, current = service.change_password(original, PASSWORD, replacement, replacement)
        issued = competing.result(timeout=5)
    if issued is not None:
        with pytest.raises(AuthError) as stale:
            service.get_session(issued)
        assert stale.value.status == 401
    assert service.get_session(current).username == "test-admin"
    assert service.login("test-admin", replacement)[0].username == "test-admin"
