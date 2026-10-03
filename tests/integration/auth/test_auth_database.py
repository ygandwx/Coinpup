"""Run against the disposable migrated PostgreSQL database only."""

import os
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pytest
from coinpup_api.admin import create_admin, reset_admin_password
from coinpup_api.auth import AuthError, AuthService, create_auth_router
from coinpup_api.config import Settings
from coinpup_api.database import Database
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
