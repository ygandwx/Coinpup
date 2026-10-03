"""Single-administrator sessions with a transactionally enforced login throttle."""

import logging
import math
from dataclasses import dataclass
from datetime import timedelta
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, Field, SecretStr
from sqlalchemy import Engine, func, select, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from coinpup_api.config import Settings
from coinpup_api.models import Administrator, AuthSession, LoginGuard
from coinpup_api.security import (
    DUMMY_PASSWORD_HASH,
    PASSWORD_HASHER,
    csrf_token_for,
    new_session_token,
    token_digest,
    valid_csrf_token,
    verify_password,
)

logger = logging.getLogger("coinpup.auth")


class AuthError(Exception):
    def __init__(self, code: str, status: int, retry_after: int | None = None):
        super().__init__(code)
        self.code = code
        self.status = status
        self.retry_after = retry_after


@dataclass(frozen=True)
class Identity:
    id: UUID
    username: str


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: SecretStr = Field(min_length=1, max_length=128)


class UserResponse(BaseModel):
    id: UUID
    username: str


class SessionResponse(BaseModel):
    user: UserResponse
    csrf_token: str


def lock_login_guard(session: Session) -> LoginGuard:
    guard = session.scalar(select(LoginGuard).where(LoginGuard.id == 1).with_for_update())
    if guard is None:
        raise AuthError("authentication_unavailable", 503)
    return guard


class AuthService:
    def __init__(self, settings: Settings, engine: Engine | None):
        self.settings = settings
        self.engine = engine

    def require_engine(self) -> Engine:
        if self.engine is None:
            raise AuthError("authentication_unavailable", 503)
        return self.engine

    def login(self, username: str, password: str, previous_token: str | None = None):
        failure = None
        result = None
        with Session(self.require_engine()) as session, session.begin():
            guard = lock_login_guard(session)
            now = session.scalar(select(func.clock_timestamp()))
            if guard.locked_until is not None and guard.locked_until > now:
                wait = math.ceil((guard.locked_until - now).total_seconds())
                raise AuthError("login_rate_limited", 429, wait)
            if (
                guard.window_started_at is None
                or now - guard.window_started_at
                >= timedelta(seconds=self.settings.login_window_seconds)
                or guard.locked_until is not None
            ):
                guard.failure_count = 0
                guard.window_started_at = now
                guard.locked_until = None
            administrator = session.scalar(select(Administrator).with_for_update())
            encoded = administrator.password_hash if administrator else DUMMY_PASSWORD_HASH
            password_ok = verify_password(encoded, password)
            username_ok = (
                administrator is not None and administrator.username == username.strip().lower()
            )
            if not password_ok or not username_ok:
                guard.failure_count += 1
                if guard.failure_count >= self.settings.login_max_failures:
                    guard.locked_until = now + timedelta(seconds=self.settings.login_lock_seconds)
                    failure = AuthError("login_rate_limited", 429, self.settings.login_lock_seconds)
                else:
                    failure = AuthError("invalid_credentials", 401)
            else:
                guard.failure_count = 0
                guard.window_started_at = None
                guard.locked_until = None
                if PASSWORD_HASHER.check_needs_rehash(encoded):
                    administrator.password_hash = PASSWORD_HASHER.hash(password)
                    administrator.updated_at = now
                if previous_token:
                    session.execute(
                        update(AuthSession)
                        .where(AuthSession.token_hash == token_digest(previous_token))
                        .values(revoked_at=now)
                    )
                token = new_session_token()
                session.add(
                    AuthSession(
                        token_hash=token_digest(token),
                        administrator_id=administrator.id,
                        created_at=now,
                        expires_at=now + timedelta(seconds=self.settings.session_ttl_seconds),
                    )
                )
                result = (Identity(administrator.id, administrator.username), token)
        # Raise after COMMIT: failed attempts must survive the request's error response.
        if failure:
            raise failure
        return result

    def get_session(self, token: str | None) -> Identity:
        if not token or len(token) > 128:
            raise AuthError("authentication_required", 401)
        with Session(self.require_engine()) as session:
            administrator = session.scalar(
                select(Administrator)
                .join(AuthSession, AuthSession.administrator_id == Administrator.id)
                .where(
                    AuthSession.token_hash == token_digest(token),
                    AuthSession.revoked_at.is_(None),
                    AuthSession.expires_at > func.now(),
                )
            )
            if administrator is None:
                raise AuthError("authentication_required", 401)
            return Identity(administrator.id, administrator.username)

    def logout(self, token: str) -> None:
        with Session(self.require_engine()) as session, session.begin():
            session.execute(
                update(AuthSession)
                .where(AuthSession.token_hash == token_digest(token))
                .values(revoked_at=func.now())
            )


def cookie_name(settings: Settings) -> str:
    return "__Host-coinpup_session" if settings.environment == "production" else "coinpup_session"


def check_origin(request: Request, settings: Settings) -> None:
    if request.headers.get("origin") not in settings.allowed_origins:
        raise HTTPException(403, "origin_not_allowed", headers={"Cache-Control": "no-store"})


def translate_error(error: AuthError) -> HTTPException:
    headers = {"Cache-Control": "no-store"}
    if error.retry_after is not None:
        headers["Retry-After"] = str(error.retry_after)
    return HTTPException(error.status, error.code, headers=headers)


def create_auth_router(settings: Settings, engine: Engine | None) -> APIRouter:
    service = AuthService(settings, engine)
    router = APIRouter(prefix="/api/v1/auth", tags=["authentication"])
    name = cookie_name(settings)
    secure = settings.environment == "production"

    def execute(operation, *args):
        try:
            return operation(*args)
        except AuthError as error:
            raise translate_error(error) from None
        except SQLAlchemyError:
            logger.warning("Authentication database operation failed")
            raise HTTPException(
                503, "authentication_unavailable", headers={"Cache-Control": "no-store"}
            ) from None

    def payload(identity: Identity, token: str) -> SessionResponse:
        return SessionResponse(
            user=UserResponse(id=identity.id, username=identity.username),
            csrf_token=csrf_token_for(token),
        )

    @router.post("/login", response_model=SessionResponse)
    def login(body: LoginRequest, request: Request, response: Response) -> SessionResponse:
        check_origin(request, settings)
        # Changing an existing authenticated browser session is a protected write as well.
        previous = request.cookies.get(name)
        if previous:
            try:
                service.get_session(previous)
            except AuthError as error:
                if error.status != 401:
                    raise translate_error(error) from None
            except SQLAlchemyError:
                raise HTTPException(503, "authentication_unavailable") from None
            else:
                if not valid_csrf_token(previous, request.headers.get("x-csrf-token")):
                    raise HTTPException(403, "csrf_failed", headers={"Cache-Control": "no-store"})
        identity, token = execute(
            service.login, body.username, body.password.get_secret_value(), previous
        )
        response.set_cookie(
            name,
            token,
            max_age=settings.session_ttl_seconds,
            httponly=True,
            secure=secure,
            samesite="strict",
            path="/",
        )
        response.headers["Cache-Control"] = "no-store"
        return payload(identity, token)

    @router.get("/session", response_model=SessionResponse)
    def current_session(request: Request, response: Response) -> SessionResponse:
        token = request.cookies.get(name)
        identity = execute(service.get_session, token)
        response.headers["Cache-Control"] = "no-store"
        return payload(identity, token)

    @router.post("/logout", status_code=204)
    def logout(request: Request) -> Response:
        check_origin(request, settings)
        token = request.cookies.get(name)
        execute(service.get_session, token)
        if not valid_csrf_token(token, request.headers.get("x-csrf-token")):
            raise HTTPException(403, "csrf_failed", headers={"Cache-Control": "no-store"})
        execute(service.logout, token)
        response = Response(status_code=204, headers={"Cache-Control": "no-store"})
        response.delete_cookie(name, path="/", httponly=True, secure=secure, samesite="strict")
        return response

    return router
